"""
Unified ``/v1/*`` public API surface.

Routes the OpenAI-compatible and Anthropic-compatible endpoints. Defaults
to the vLLM backend (via ``vllm_proxy``); chat / responses / messages /
count_tokens additionally dispatch to Azure OpenAI (via ``azure_proxy``)
when the requested ``model`` alias is configured under ``[azure_models.*]``
AND the caller has ``can_use_azure`` (or is admin). One base URL lets clients
like Claude Code's model picker see both backends side-by-side.

Design: each base URL has its own default fallback. ``/v1/*`` serves
vLLM-first (Azure is additive when the caller is authorized); aliases the
caller can't reach — whether typed wrong, or an Azure alias requested by a
user without access — silently fall back through ``_resolve_model`` to the
vLLM default. ``/azure/v1/*`` (see ``azure_api.py``) is the opposite: Azure
is the only backend there, with its own ``AZURE_FALLBACK_MAP``. A request's
fallback target is therefore determined by where it came in, not by what
permissions the user happens to lack — which matches the "be liberal with
unknown aliases" stance the gateway has always taken.

Every route is also exposed without the ``/v1`` prefix (``/chat/completions``,
``/models``, ``/embeddings``, ``/rerank``, ``/score``, ``/chat/completions/render``,
``/systemone`` alongside the canonical ``/v1/...`` paths; the Anthropic-shaped ``/messages``,
``/messages/count_tokens``, ``/responses``, ``/tokenize`` already had this alias). Clients whose base URL
omits ``/v1`` reach the same handler — a common Roo Code / Cline / Cursor
misconfiguration that previously surfaced as a silent 404.
"""

from __future__ import annotations

import re
from datetime import datetime

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import (
    _MODEL_METADATA_KEYS,
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    get_cloud_budget_fallback,
    get_concurrency_settings,
    get_model_routing_snapshot,
)
from app.core.deps import (
    _enforce_daily_limit,
    check_cloud_budget,
    effective_cloud_limit,
    get_current_user,
    get_current_user_unmetered,
    get_today_usage,
    get_usage_history,
    limited_current_user,
)
from app.core.logger import logger
from app.core.timeutil import (
    LOCAL_TZ,
    local_month_start,
    next_local_midnight,
    seconds_until_local_midnight,
)
from app.models.schema import User
from app.services.azure_proxy import (
    azure_forward_chat_completions,
    azure_forward_count_tokens,
    azure_forward_messages,
    azure_forward_responses,
)
from app.services import client_rules, guardrail, model_catalog, terminal
from app.services.bedrock_proxy import (
    bedrock_forward_chat_completions,
    bedrock_forward_count_tokens,
    bedrock_forward_messages,
)
from app.services.vllm_proxy import (
    vllm_forward_chat_completions,
    vllm_forward_count_tokens,
    vllm_forward_messages,
    vllm_forward_render,
    vllm_forward_responses,
    vllm_forward_simple_request,
    vllm_forward_systemone,
    vllm_forward_tokenize,
)

router = APIRouter()


async def _peek_model_alias(request: Request) -> str:
    """Read the ``model`` field from the request body without consuming it.

    FastAPI's ``Request`` caches the raw body bytes on first read, so a
    downstream call to ``request.json()`` inside ``forward_*`` will still
    succeed. Returns ``""`` on a body that isn't JSON or doesn't carry
    ``model`` so the downstream handler can produce its own error.
    """
    try:
        body = await request.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    alias = body.get("model")
    return alias if isinstance(alias, str) else ""


def _route_to_azure(alias: str, user: User) -> bool:
    """Whether ``alias`` should be dispatched to the Azure backend.

    Returns True only when the alias is configured under ``[azure_models.*]``
    AND the caller has Azure access. Otherwise returns False, letting the
    vLLM handler take it — which, for an Azure-only alias from a user
    without ``can_use_azure``, means the request quietly falls back through
    ``_resolve_model`` to the vLLM default. That preserves the gateway's
    "be liberal with unknown aliases" stance: from the caller's perspective
    the Azure alias just behaves like any other unknown name on ``/v1/*``.

    Azure existence is still hidden via ``GET /v1/models`` (which omits
    Azure entries for users without permission), so this fallback only
    matters when a client explicitly forces an alias it shouldn't have
    seen — at which point a forgiving fallback is friendlier than a 404.
    """
    azure_models = get_azure_models_snapshot()
    if alias not in azure_models:
        return False
    if user.can_use_azure or user.is_admin:
        return True
    # Azure alias requested by user without access — fall through to the
    # vLLM handler so its _resolve_model can route to the configured
    # vLLM-side default. Logged so operators can spot clients that keep
    # asking for an alias they don't have access to.
    logger.info(
        "Azure alias '{}' requested by user '{}' without can_use_azure; "
        "falling back to vLLM default",
        alias, user.username,
    )
    return False


def _route_to_bedrock(alias: str, user: User) -> bool:
    """Whether ``alias`` should be dispatched to the Bedrock backend.

    Same contract as ``_route_to_azure``: True only when the alias is
    configured under ``[bedrock_models.*]`` AND the caller has Bedrock
    access; otherwise the request stays on the vLLM path and falls back
    through ``_resolve_model`` like any unknown alias.
    """
    bedrock_models = get_bedrock_models_snapshot()
    if alias not in bedrock_models:
        return False
    if user.can_use_bedrock or user.is_admin:
        return True
    logger.info(
        "Bedrock alias '{}' requested by user '{}' without can_use_bedrock; "
        "falling back to vLLM default",
        alias, user.username,
    )
    return False


# Response header set when a cloud-bound request was served on-prem because
# the user's daily cloud cap was exhausted (see
# ``_cloud_budget_gate``). Distinct from ``X-Model-Fallback`` (which the vLLM
# resolver adds for the alias → default rewrite) so a client can tell "the
# server you asked for was down" from "your cloud budget is spent".
_BUDGET_FALLBACK_HEADER = "X-Budget-Fallback"


async def _cloud_budget_gate(user: User, backend: str, alias: str, endpoint: str) -> bool:
    """Enforce the daily cloud cap for a ``/v1/*`` request
    that ``_route_to_azure`` / ``_route_to_bedrock`` has already claimed.

    Returns True when the request may go to ``backend``.

    When the cap is exhausted, the outcome is the operator's call
    (``[app].cloud_budget_fallback``, admin panel → Cloud Budget Fallback):
      - off (default): HTTP 429, so the caller knows their cloud budget is
        gone rather than silently getting a different model's output;
      - on: returns False and the handler serves the request from the
        on-prem vLLM path (``_resolve_model`` maps the cloud alias to the
        configured vLLM default). The overall ``daily_limit_usd`` was already
        enforced at auth, so once on-prem budget is gone too the request
        never gets this far — it is refused with the usual 429.

    Blocking DB read → threadpool.
    """
    allow = get_cloud_budget_fallback()
    ok = await run_in_threadpool(
        check_cloud_budget, user, backend, allow_fallback=allow
    )
    if not ok:
        logger.warning(
            "{} daily limit exhausted | user={} model={} endpoint={} "
            "-> falling back to on-prem (cloud_budget_fallback=true)",
            backend.capitalize(), user.username, alias, endpoint,
        )
    return ok


def _tag_budget_fallback(response: Response, backend: str) -> Response:
    """Mark a response served on-prem because the cloud cap was spent."""
    response.headers[_BUDGET_FALLBACK_HEADER] = (
        f"{backend} daily limit exceeded; served by on-prem"
    )
    return response


# Capability labels for the /v1/models response
_TYPE_CAPABILITIES: dict[str, str] = {
    "llm": "text-generation",
    "vlm": "vision-language",
    "embedding": "embeddings",
    "vision_embedding": "vision-embeddings",
    "reranker": "reranking",
    "vision_reranker": "vision-reranking",
}


@router.get("/v1/models")
@router.get("/models")
async def list_models(user: User = Depends(get_current_user)):
    """List chat-capable models available to this user.

    vLLM entries are always included. Azure entries are merged in only when
    the user has ``can_use_azure`` (or is admin) so an Anthropic / OpenAI
    client pointed at this single base URL sees exactly the set of aliases
    its API key may actually use.

    Entries marked ``hidden`` are left out, here as on the dashboard, so a
    model can be staged or retired without showing up in client pickers
    (Claude Code, Roo Code). Hiding is a listing concern only: requests that
    name the alias are still served.
    """
    models = []
    for name, route in get_model_routing_snapshot().items():
        model_type = route["type"]
        if model_type not in ("llm", "vlm") or route.get("hidden"):
            continue
        entry: dict[str, object] = {
            "id": name,
            "object": "model",
            "owned_by": "llm-gateway",
            "type": model_type,
            "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
        }
        # Surface optional per-model metadata (context_window,
        # max_output_tokens, supports_*, display_name, ...) when it's
        # declared in config.toml. Any missing key is simply omitted so
        # existing configs — which don't set any of these — produce the
        # exact same response they did before.
        for meta_key in _MODEL_METADATA_KEYS:
            if meta_key in route:
                entry[meta_key] = route[meta_key]
        models.append(entry)

    if user.can_use_azure or user.is_admin:
        for alias, route in get_azure_models_snapshot().items():
            model_type = route.get("type", "llm")
            if model_type not in ("llm", "vlm") or route.get("hidden"):
                continue
            entry = {
                "id": alias,
                "object": "model",
                "owned_by": "azure-openai",
                "type": model_type,
                "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
            }
            for meta_key in _MODEL_METADATA_KEYS:
                if meta_key in route:
                    entry[meta_key] = route[meta_key]
            models.append(entry)

    if user.can_use_bedrock or user.is_admin:
        for alias, route in get_bedrock_models_snapshot().items():
            model_type = route.get("type", "llm")
            if model_type not in ("llm", "vlm") or route.get("hidden"):
                continue
            entry = {
                "id": alias,
                "object": "model",
                "owned_by": "aws-bedrock",
                "type": model_type,
                "capability": _TYPE_CAPABILITIES.get(model_type, model_type),
            }
            for meta_key in _MODEL_METADATA_KEYS:
                if meta_key in route:
                    entry[meta_key] = route[meta_key]
            models.append(entry)

    return {"object": "list", "data": models}


def _usd(value: float) -> float:
    """Round to the precision ``usage_logs.cost_usd`` stores (Numeric(12, 6))."""
    return round(value, 6)


def _remaining(limit: float, used: float) -> float:
    return _usd(max(0.0, limit - used))


class _Output:
    """How an account endpoint should answer: JSON (the default) or the
    plain-text view for a terminal (``app/services/terminal.py``).

    Text is chosen with ``?format=text`` or an ``Accept`` header that names
    ``text/plain`` (curl's default ``*/*`` stays JSON, so nothing that reads
    these endpoints today changes). ``color=1`` adds ANSI colours and
    ``ascii=1`` keeps the drawing to ASCII: the caller decides both, since
    only it knows whether it is writing to a terminal and in which locale.
    """

    def __init__(
        self,
        request: Request,
        fmt: str | None = Query(
            None, alias="format", pattern="^(json|text)$",
            description="`json` (default) or `text`: a plain-text view for a terminal.",
        ),
        color: bool = Query(False, description="With `format=text`: ANSI colours."),
        ascii_only: bool = Query(False, alias="ascii", description="With `format=text`: ASCII only."),
    ):
        if fmt is None:
            fmt = "text" if "text/plain" in request.headers.get("accept", "").lower() else "json"
        self.text = fmt == "text"
        self.style = terminal.Style(color=color, ascii=ascii_only)

    def reply(self, data: dict, render) -> Response | dict:
        if not self.text:
            return data
        return PlainTextResponse(render(data, self.style), headers={"Cache-Control": "no-store"})


@router.get("/v1/me")
@router.get("/me")
async def account_info(
    user: User = Depends(get_current_user_unmetered),
    output: _Output = Depends(),
):
    """The caller's own access and today's budget, for clients and scripts.

    ``?format=text`` (or ``Accept: text/plain``) answers with the same
    figures laid out for a terminal; see ``GET /v1/cli`` for a script that
    asks without typing the key.

    Answers even once the daily limit is spent (no 429 at auth), since
    "how much is left / when does it reset" is what a client asks then.
    Every account has a finite limit, so every amount is a number (never
    null, no "unlimited").

    Per backend (``onprem`` = vLLM, ``azure``, ``aws`` = Bedrock):
      - ``access``: may this key reach it (admins bypass the cloud flags);
      - ``used_usd``: today's spend there;
      - ``remaining_usd``: what can still be spent there today — the tighter
        of the cloud cap (azure / aws) and the overall daily limit (cloud
        spend counts toward both), 0 without access.

    ``cloud`` appears only when the caller may use a cloud backend and has a
    cloud cap (``effective_cloud_limit``: their own, else the gateway-wide
    default): Azure + AWS spend today against it.

    Limits are those the gateway enforces; with ``ENFORCE_DAILY_LIMIT=false``
    they are only logged (``daily.enforced`` = false).
    """
    today = await run_in_threadpool(get_today_usage, user)

    daily_limit = max(0.0, user.daily_limit_usd)
    daily_remaining = _remaining(daily_limit, today["total_cost_usd"])

    azure_access = user.can_use_azure or user.is_admin
    bedrock_access = user.can_use_bedrock or user.is_admin
    # The cap only means something to a user who may reach a cloud backend.
    cloud_limit = effective_cloud_limit(user) if (azure_access or bedrock_access) else None
    cloud_used = today["azure_cost_usd"] + today["bedrock_cost_usd"]
    cloud_remaining = (
        daily_remaining if cloud_limit is None
        else min(daily_remaining, _remaining(cloud_limit, cloud_used))
    )

    def _backend(access: bool, used: float, remaining: float) -> dict:
        return {
            "access": access,
            "used_usd": _usd(used),
            "remaining_usd": remaining if access else 0.0,
        }

    mode, limit = get_concurrency_settings()
    concurrency_applies = mode != "off" and not user.is_admin and not user.concurrency_waived
    local_now = datetime.now(LOCAL_TZ)

    return output.reply({
        "object": "account",
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "daily": {
            "limit_usd": daily_limit,
            "used_usd": _usd(today["total_cost_usd"]),
            "remaining_usd": daily_remaining,
            "requests": today["total_requests"],
            "input_tokens": today["total_input_tokens"],
            "output_tokens": today["total_output_tokens"],
            "enforced": _enforce_daily_limit(),
            "timezone": str(LOCAL_TZ),
            "resets_at": next_local_midnight(local_now).isoformat(),
            "resets_in_seconds": seconds_until_local_midnight(),
        },
        "backends": {
            "onprem": _backend(True, today["vllm_cost_usd"], daily_remaining),
            "azure": _backend(
                azure_access,
                today["azure_cost_usd"],
                cloud_remaining,
            ),
            "aws": _backend(
                bedrock_access,
                today["bedrock_cost_usd"],
                cloud_remaining,
            ),
        },
        **({"cloud": {
            "limit_usd": cloud_limit,
            "used_usd": _usd(cloud_used),
            "remaining_usd": cloud_remaining,
        }} if cloud_limit is not None else {}),
        "cloud_budget_fallback": get_cloud_budget_fallback(),
        "concurrency": (
            {"mode": mode, "limit": limit} if concurrency_applies else {"mode": "off"}
        ),
    }, terminal.render_me)


@router.get("/v1/status")
async def model_status(
    user: User = Depends(get_current_user_unmetered),
    output: _Output = Depends(),
):
    """The models this key can use and whether they are up, for a terminal
    or a script: what the dashboard's model tables show.

    ``models`` lists every model the account can see (hidden entries and
    cloud backends without access left out), in the dashboard's order, each
    with its billed prices per 1M tokens. On-prem models carry ``state``
    ``up`` / ``down`` from the gateway's health loop, and ``running`` /
    ``waiting`` request counts when the server reports them; cloud models
    are ``unchecked`` (the gateway does not probe them). ``onprem`` sums
    the on-prem ones up. A key is never omitted as null: it is left out.

    Like ``/v1/me`` it answers once the daily limit is spent, and
    ``?format=text`` gives the terminal view. No database read.
    """
    models = model_catalog.visible_models(user)
    onprem = [m for m in models if m["backend"] == "onprem"]
    return output.reply({
        "object": "status",
        "onprem": {
            "total": len(onprem),
            "up": sum(1 for m in onprem if m["state"] == "up"),
            "down": [m["id"] for m in onprem if m["state"] == "down"],
        },
        "models": models,
    }, terminal.render_status)


USAGE_MAX_DAYS = 90


@router.get("/v1/usage")
def usage_history(
    days: int = Query(7, ge=1, le=USAGE_MAX_DAYS, description="How many days back, today included."),
    user: User = Depends(get_current_user_unmetered),
    output: _Output = Depends(),
):
    """The caller's own usage: one row per day and this month by model,
    the figures behind the dashboard's trend chart and Cost by Model card.

    ``days`` are Asia/Taipei calendar days, oldest first, today last and
    still running; quiet days are present as zeros. ``month`` is the local
    month so far, with one ``models`` row per model and backend in the
    dashboard's order. Like the dashboard, the month leaves out a cloud
    backend the account no longer has access to; the days count everything
    (as the daily budget does).

    Answers once the daily limit is spent; ``?format=text`` gives the
    terminal view. Plain ``def``: it reads the database and awaits nothing.
    """
    history = get_usage_history(user, days)
    hidden = model_catalog.hidden_cloud_backends(user)
    catalog = model_catalog.current_catalog_keys()
    rows = [r for r in history["models"] if r["backend"] not in hidden]
    extra = sorted({r["backend"] for r in rows} - set(model_catalog.BACKEND_ORDER))
    backend_rank = {name: i for i, name in enumerate((*model_catalog.BACKEND_ORDER, *extra))}
    rows.sort(key=lambda r: (backend_rank[r["backend"]], model_catalog.usage_row_key(r, catalog)))
    return output.reply({
        "object": "usage",
        "timezone": str(LOCAL_TZ),
        "days": [
            {
                "date": day["date"],
                "requests": day["reqs"],
                "input_tokens": day["input_tokens"],
                "output_tokens": day["output_tokens"],
                "cost_usd": day["cost"],
            }
            for day in history["days"]
        ],
        "month": {
            "month": local_month_start().strftime("%Y-%m"),
            "requests": sum(r["requests"] for r in rows),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "cost_usd": _usd(sum(r["cost_usd"] for r in rows)),
            "models": [
                {
                    "model": r["model"],
                    "backend": model_catalog.BACKEND_NAMES.get(r["backend"], r["backend"]),
                    "type": r["type"],
                    "requests": r["requests"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cost_usd": r["cost_usd"],
                }
                for r in rows
            ],
        },
    }, terminal.render_usage)


_CLI_SCRIPT = Path(__file__).resolve().parent.parent.parent / "setup" / "gw"
_CLI_URL_PLACEHOLDER = "__GATEWAY_URL__"
# Only a plain host[:port] is ever written into the script: the value comes
# from the Host header.
_CLI_HOST = re.compile(r"^(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+(?::\d{1,5})?$")


@router.get("/v1/cli")
def cli_script(request: Request, user: User = Depends(get_current_user_unmetered)):
    """``gw``, the shell script that shows ``/v1/me``, ``/v1/status`` and
    ``/v1/usage`` in a terminal (``setup/gw``), with this gateway's address
    filled in. Save it somewhere on ``PATH`` and make it executable.

    The script holds no key: it reads the one Claude Code already has
    (``ANTHROPIC_AUTH_TOKEN`` in ``~/.claude/settings.json``) or
    ``LLM_GATEWAY_API_KEY``. It is the same for every account, and still
    asks for a key like every API route, so the IP allowlist covers it.
    """
    if not _CLI_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="The gw script is not installed on this gateway.")
    host = request.headers.get("host", "")
    scheme = request.url.scheme if request.url.scheme in ("http", "https") else "https"
    base = f"{scheme}://{host}" if _CLI_HOST.match(host) else ""
    script = _CLI_SCRIPT.read_text(encoding="utf-8").replace(_CLI_URL_PLACEHOLDER, base)
    return PlainTextResponse(script, headers={"Content-Disposition": 'inline; filename="gw"'})


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request, user: User = Depends(limited_current_user)):
    alias = await _peek_model_alias(request)
    budget_fallback: str | None = None
    if alias and _route_to_azure(alias, user):
        # Azure-bound: enforce the daily cloud cap. On exceed
        # this is a 429 unless the operator enabled cloud_budget_fallback,
        # in which case the request drops through to the vLLM path below.
        if await _cloud_budget_gate(user, "azure", alias, "/v1/chat/completions"):
            return await azure_forward_chat_completions(request, user)
        budget_fallback = "azure"
    elif alias and _route_to_bedrock(alias, user):
        # Same contract as the Azure gate above, for Bedrock.
        if await _cloud_budget_gate(user, "bedrock", alias, "/v1/chat/completions"):
            return await bedrock_forward_chat_completions(request, user)
        budget_fallback = "bedrock"
    # Guardrail: local (vLLM) traffic only, cloud-budget fallbacks included.
    blocked = await guardrail.screen(request, user, "chat_completions", "/v1/chat/completions", "vllm")
    if blocked is not None:
        return blocked
    response = await vllm_forward_chat_completions(
        request, user, allowed_types=["llm", "vlm"]
    )
    if budget_fallback:
        _tag_budget_fallback(response, budget_fallback)
    return response


# OpenAPI documentation for the render endpoint.
#
# Every handler in this router takes a raw ``Request`` (the gateway is a
# pass-through — binding a Pydantic body model would make FastAPI validate
# and silently DROP vLLM's long tail of extra fields: chat_template_kwargs,
# vllm_xargs, structured_outputs, …), so FastAPI has no body schema to infer
# and ``/docs`` renders these operations with no request body at all. That is
# tolerable for the generation endpoints, whose shape every OpenAI client
# already knows — but this one is a debug aid whose whole audience is a human
# poking at Swagger UI's "Try it out", and an empty body box is useless there.
#
# ``openapi_extra`` documents the body without binding it: the schema below is
# descriptive only, nothing here is enforced. It names the fields that actually
# change what gets rendered rather than mirroring vLLM's full
# ChatCompletionRequest (which drifts per vLLM release — copying it would be a
# promise this gateway can't keep). ``additionalProperties: true`` is the honest
# statement of the contract: anything else you send is forwarded verbatim.
_RENDER_REQUEST_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["model", "messages"],
    "additionalProperties": True,
    "description": (
        "A standard OpenAI chat completions body. Only the fields that affect "
        "rendering are documented here; any other field (sampling knobs, "
        "vLLM extensions such as `vllm_xargs` or `structured_outputs`, …) is "
        "forwarded to the downstream verbatim and shows up in the resolved "
        "`sampling_params` of the response."
    ),
    "properties": {
        "model": {
            "type": "string",
            "description": "Model alias as listed by `GET /v1/models`.",
        },
        "messages": {
            "type": "array",
            "description": "Chat messages, exactly as for `/v1/chat/completions`.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "tools": {
            "type": "array",
            "description": (
                "Tool definitions. Included in the render because the chat "
                "template is what turns them into prompt text."
            ),
            "items": {"type": "object", "additionalProperties": True},
        },
        "tool_choice": {"description": "`none` | `auto` | `required` | a tool object."},
        "documents": {
            "type": "array",
            "description": "RAG documents, for templates that render them.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "chat_template": {
            "type": "string",
            "description": "Override the model's chat template for this render.",
        },
        "chat_template_kwargs": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Extra variables passed to the chat template — e.g. "
                "`{\"enable_thinking\": false}` on Qwen3."
            ),
        },
        "mm_processor_kwargs": {
            "type": "object",
            "additionalProperties": True,
            "description": "Multi-modal processor options (VLM routes).",
        },
        "add_generation_prompt": {
            "type": "boolean",
            "default": True,
            "description": "Append the assistant generation prefix, as generation would.",
        },
        "continue_final_message": {
            "type": "boolean",
            "default": False,
            "description": "Continue the last assistant message instead of starting a new turn.",
        },
        "temperature": {"type": "number"},
        "top_p": {"type": "number"},
        "max_tokens": {"type": "integer"},
        "stop": {"description": "String or array of stop sequences."},
        "reasoning_effort": {
            "type": "string",
            "description": "Passed through; visible in the resolved sampling_params.",
        },
    },
    "example": {
        "model": "your-model-alias",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ],
        "add_generation_prompt": True,
        "temperature": 0.7,
    },
}

_RENDER_OPENAPI: dict[str, object] = {
    "summary": "Render a chat request without generating (debug)",
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": _RENDER_REQUEST_SCHEMA,
                # Named examples become a dropdown in Swagger UI — the three
                # questions this endpoint actually gets asked.
                "examples": {
                    "minimal": {
                        "summary": "What does the template do to my messages?",
                        "value": {
                            "model": "your-model-alias",
                            "messages": [
                                {"role": "system", "content": "You are a helpful assistant."},
                                {"role": "user", "content": "Hello!"},
                            ],
                        },
                    },
                    "with_tools": {
                        "summary": "How are my tools rendered into the prompt?",
                        "description": (
                            "Tool definitions are prompt text after the chat "
                            "template runs — this is how you see what the model "
                            "is actually told about them."
                        ),
                        "value": {
                            "model": "your-model-alias",
                            "messages": [{"role": "user", "content": "What is the weather?"}],
                            "tools": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "description": "Get the weather for a city.",
                                        "parameters": {
                                            "type": "object",
                                            "properties": {"city": {"type": "string"}},
                                            "required": ["city"],
                                        },
                                    },
                                }
                            ],
                        },
                    },
                    "template_kwargs": {
                        "summary": "Did my chat_template_kwargs take effect?",
                        "description": (
                            "Template switches such as Qwen3's `enable_thinking` "
                            "only show up in the rendered prompt — compare two "
                            "renders to confirm the flag did anything."
                        ),
                        "value": {
                            "model": "your-model-alias",
                            "messages": [{"role": "user", "content": "Hello!"}],
                            "chat_template_kwargs": {"enable_thinking": False},
                        },
                    },
                },
            }
        },
    },
    "responses": {
        "200": {
            "description": (
                "The rendered request, as a single object — whatever the "
                "downstream vLLM returned, with `model` swapped back to the "
                "alias you asked for.\n\n"
                "| field | meaning |\n"
                "|---|---|\n"
                "| `decoded_prompt` | The prompt as **text**. Added by the "
                "gateway (vLLM does not return it); omitted when "
                "`?decode=false`. This is the field you usually want. |\n"
                "| `token_ids` | The prompt as token **IDs** — integers, not "
                "text. What the engine is actually handed. |\n"
                "| `sampling_params` | The parameters the engine would run "
                "with, defaults filled in — so you can see what your request "
                "resolved to. |\n"
                "| `decode_error` | Present only when `decode` was asked for "
                "and failed; the render itself is still returned. |\n"
            ),
            "content": {
                "application/json": {
                    "examples": {
                        "default": {
                            "summary": "Default (decode on)",
                            "value": {
                                "request_id": "chatcmpl-b1f0c2e4",
                                "model": "your-model-alias",
                                "decoded_prompt": (
                                    "<|im_start|>system\nYou are a helpful "
                                    "assistant.<|im_end|>\n<|im_start|>user\n"
                                    "Hello!<|im_end|>\n<|im_start|>assistant\n"
                                ),
                                "token_ids": [151644, 8948, 198, 2610, 525],
                                "sampling_params": {
                                    "temperature": 0.7,
                                    "top_p": 1.0,
                                    "max_tokens": 4096,
                                },
                            },
                        },
                        "decode_false": {
                            "summary": "?decode=false — exactly what vLLM sent",
                            "value": {
                                "request_id": "chatcmpl-b1f0c2e4",
                                "model": "your-model-alias",
                                "token_ids": [151644, 8948, 198, 2610, 525],
                                "sampling_params": {"temperature": 0.7},
                            },
                        },
                        "decode_failed": {
                            "summary": "Detokenize failed — render still returned",
                            "value": {
                                "request_id": "chatcmpl-b1f0c2e4",
                                "model": "your-model-alias",
                                "token_ids": [151644, 8948, 198, 2610, 525],
                                "decode_error": "detokenize returned HTTP 404",
                            },
                        },
                    }
                }
            },
        },
        "404": {
            "description": (
                "Propagated from the downstream. Most likely its vLLM is too "
                "old to serve `/chat/completions/render` — the endpoint "
                "arrived with the disaggregated-serving render server "
                "(vllm-project/vllm#36166). Nothing to configure on the "
                "gateway; the model has to be on a newer vLLM."
            )
        },
        "429": {"description": "Daily spend limit exceeded (this call is not itself billed)."},
        "502": {"description": "Downstream unreachable."},
    },
}


@router.post("/v1/chat/completions/render", openapi_extra=_RENDER_OPENAPI)
@router.post("/chat/completions/render", openapi_extra=_RENDER_OPENAPI)
async def chat_completions_render(
    request: Request,
    decode: bool = Query(
        True,
        description=(
            "Return the rendered prompt as text under `decoded_prompt` "
            "(default). Costs one extra call to the same server's "
            "`/detokenize`; on failure the render is still returned, with the "
            "reason under `decode_error`. Pass `false` for a pure "
            "pass-through of what vLLM returned."
        ),
    ),
    user: User = Depends(get_current_user),
):
    """See exactly what the model receives — the chat template applied to your
request, **without generating anything**.

Post the same body you would post to `/v1/chat/completions`; you get back the
prompt that request renders into, plus the sampling parameters it resolves to.
Nothing is generated, so it is fast and **not billed**.

### Use it when

* the model behaves as if it saw something other than what you sent
* you want to check how your `tools` are turned into prompt text
* you are tuning `chat_template_kwargs` (e.g. Qwen3's `enable_thinking`) and
  need to confirm the switch actually changed the prompt
* you want the resolved `sampling_params` — the defaults your request inherits

### Reading the response

`decoded_prompt` is the prompt as text and is usually the only field you need.
vLLM itself returns token IDs only, so the gateway detokenizes them for you;
pass `?decode=false` to skip that and get exactly what vLLM sent. If the
detokenize step fails the render is still returned, with the reason in
`decode_error`.

### Try it

```bash
curl -s "$GATEWAY/v1/chat/completions/render" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "your-model-alias",
       "messages": [{"role": "user", "content": "Hello!"}]}' \
  | jq -r .decoded_prompt
```

With the OpenAI SDK there is no typed method for this endpoint — use the raw
request escape hatch, which keeps your existing base URL and key:

```python
import httpx
resp = client.post("/chat/completions/render", cast_to=httpx.Response,
                   body={"model": "your-model-alias",
                         "messages": [{"role": "user", "content": "Hello!"}]})
print(resp.json()["decoded_prompt"])
```

### Limits

* **On-prem vLLM only.** Azure and Bedrock have no rendering surface, so a
  cloud alias posted here is not dispatched to them — it falls back to a vLLM
  model like any alias the vLLM side doesn't know (same as `/v1/tokenize`).
* Any field not listed in the schema below is **forwarded verbatim**; this is
  a pass-through, not a validated API.
* A downstream on an older vLLM answers 404 — see the response below.
"""
    return await vllm_forward_render(
        request, user, allowed_types=["llm", "vlm"], decode=decode
    )


@router.post("/v1/responses")
@router.post("/responses")
async def responses(request: Request, user: User = Depends(limited_current_user)):
    """OpenAI Responses API endpoint.

    Dispatches to the Azure backend (pure Responses pass-through) when the
    requested ``model`` alias is Azure-configured and the user has permission;
    otherwise forwards to vLLM. Same Azure dispatch rule as
    ``/v1/chat/completions``; see ``_route_to_azure``.
    """
    alias = await _peek_model_alias(request)
    budget_fallback: str | None = None
    if alias and _route_to_azure(alias, user):
        # Same cloud-cap gate as /v1/chat/completions.
        if await _cloud_budget_gate(user, "azure", alias, "/v1/responses"):
            return await azure_forward_responses(request, user)
        budget_fallback = "azure"
    # Guardrail: local (vLLM) traffic only, cloud-budget fallbacks included.
    blocked = await guardrail.screen(request, user, "responses", "/v1/responses", "vllm")
    if blocked is not None:
        return blocked
    response = await vllm_forward_responses(
        request, user, allowed_types=["llm", "vlm"], path_suffix="/responses"
    )
    if budget_fallback:
        _tag_budget_fallback(response, budget_fallback)
    return response


@router.post("/v1/messages")
@router.post("/messages")
async def messages(request: Request, user: User = Depends(limited_current_user)):
    """Anthropic Messages API compatibility endpoint.

    Translates Anthropic /v1/messages requests to OpenAI chat completions
    format, forwards to the downstream LLM/VLM, then translates the response
    back to Anthropic format.

    Both ``/v1/messages`` and ``/messages`` are accepted so that clients work
    regardless of whether their ``ANTHROPIC_BASE_URL`` already includes the
    ``/v1`` prefix (a common Claude Code / Anthropic SDK misconfiguration
    that otherwise causes a silent 404 on the direct connection).

    Dispatches to the Azure backend when the requested ``model`` alias is
    Azure-configured and the user has permission; see ``_route_to_azure``.
    """
    alias = await _peek_model_alias(request)
    reminder = await client_rules.observe_request(request, user)
    if reminder is not None:
        return reminder
    budget_fallback: str | None = None
    if alias and _route_to_azure(alias, user):
        # Same cloud-cap gate as /v1/chat/completions.
        if await _cloud_budget_gate(user, "azure", alias, "/v1/messages"):
            return await azure_forward_messages(request, user)
        budget_fallback = "azure"
    elif alias and _route_to_bedrock(alias, user):
        if await _cloud_budget_gate(user, "bedrock", alias, "/v1/messages"):
            return await bedrock_forward_messages(request, user)
        budget_fallback = "bedrock"
    # Guardrail: local (vLLM) traffic only, cloud-budget fallbacks included.
    blocked = await guardrail.screen(request, user, "messages", "/v1/messages", "vllm")
    if blocked is not None:
        return blocked
    response = await vllm_forward_messages(request, user, allowed_types=["llm", "vlm"])
    if budget_fallback:
        _tag_budget_fallback(response, budget_fallback)
    return response


@router.post("/v1/messages/count_tokens")
@router.post("/messages/count_tokens")
async def messages_count_tokens(
    request: Request, user: User = Depends(get_current_user)
):
    """Anthropic-compatible token counting endpoint.

    Used by Claude Code (and other Anthropic SDK clients) to estimate the
    input token count of a prospective request — typically for context
    window tracking. Forwards to the downstream vLLM ``/tokenize`` endpoint
    and returns ``{"input_tokens": N}``. Not billed.

    Mirrors ``/v1/messages`` by also exposing ``/messages/count_tokens`` for
    clients whose base URL already includes the ``/v1`` prefix.

    Same Azure dispatch rule as ``/v1/messages``: Azure-configured aliases
    are routed to the Azure count-tokens helper (chars/4 estimate) when the
    user has permission; otherwise vLLM's tokenizer-backed count is used.
    """
    alias = await _peek_model_alias(request)
    budget_fallback: str | None = None
    if alias and _route_to_azure(alias, user):
        # Gated like the billable Azure paths for parity with
        # /azure/v1/messages/count_tokens (whose require_azure_access
        # dependency also enforces the cloud cap). With the fallback on, the
        # count comes from the on-prem tokenizer — the same server the
        # paired /v1/messages call will be served by.
        if await _cloud_budget_gate(user, "azure", alias, "/v1/messages/count_tokens"):
            return await azure_forward_count_tokens(request, user)
        budget_fallback = "azure"
    elif alias and _route_to_bedrock(alias, user):
        if await _cloud_budget_gate(user, "bedrock", alias, "/v1/messages/count_tokens"):
            return await bedrock_forward_count_tokens(request, user)
        budget_fallback = "bedrock"
    response = await vllm_forward_count_tokens(
        request, user, allowed_types=["llm", "vlm"]
    )
    if budget_fallback:
        _tag_budget_fallback(response, budget_fallback)
    return response


@router.post("/v1/tokenize")
@router.post("/tokenize")
async def tokenize(request: Request, user: User = Depends(get_current_user)):
    """vLLM-native ``/tokenize`` pass-through.

    vLLM exposes ``/tokenize`` (not ``/v1/tokenize``) since it isn't part of
    the OpenAI API spec. The gateway accepts both paths for client convenience
    and forwards to the downstream ``/tokenize`` endpoint. Not billed.
    """
    return await vllm_forward_tokenize(
        request, user, allowed_types=["llm", "vlm"]
    )


@router.post("/v1/embeddings")
@router.post("/embeddings")
async def embeddings(request: Request, user: User = Depends(get_current_user)):
    return await vllm_forward_simple_request(
        request,
        user,
        allowed_types=["embedding", "vision_embedding"],
        path_suffix="/embeddings",
        endpoint_label="/v1/embeddings",
    )


@router.post("/v1/rerank")
@router.post("/v1/score")
@router.post("/rerank")
@router.post("/score")
async def rerank(request: Request, user: User = Depends(get_current_user)):
    return await vllm_forward_simple_request(
        request,
        user,
        allowed_types=["reranker", "vision_reranker"],
        path_suffix="/score",
        endpoint_label="/v1/score",
    )


@router.post("/v1/systemone")
@router.post("/systemone")
async def systemone(request: Request, user: User = Depends(get_current_user)):
    """System One typed decisions, in TypeSafe's ``/v1/systemone`` wire format.

    Served by ``[models.systemone.*]`` routes (e.g. Mapika's decider): post a
    ``state`` plus named ``questions`` — each a ``choice``, ``score`` or
    ``noul`` — and get calibrated probabilities back under ``answers``, from
    one forward pass. ``model`` is optional; without it the default
    systemone model answers. On-prem only, billed on input tokens (nothing
    is generated). See ``vllm_forward_systemone``.
    """
    return await vllm_forward_systemone(request, user, allowed_types=["systemone"])
