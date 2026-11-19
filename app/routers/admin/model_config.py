"""
Admin *Models* page (``/admin/models``) and the config API behind it:
``GET`` / ``PUT /admin/api/config`` and *Test connection*.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session

from app.core.auth import get_web_user
from app.core.config import (
    _MODEL_INTERNAL_KEYS,
    _MODEL_METADATA_KEYS,
    _MODEL_PRICING_KEYS,
    APP_TITLE,
    DISPLAY_ORDER_BACKENDS,
    bedrock_shared_settings,
    get_config_data,
    get_azure_models_snapshot,
    get_bedrock_models_snapshot,
    masked_config_data,
    resolve_secret,
    resolve_secret_refs,
    get_model_routing_snapshot,
    save_config,
)
from app.core.database import get_session
from app.models.schema import User
from app.routers.admin.common import router, templates
from app.services import audit


def _validate_reasoning_keys(label: str, alias: str, info: dict) -> None:
    """Type-check the per-model reasoning-effort compatibility declaration.

    `reasoning_efforts` is a list of effort names, `reasoning_effort_map` a
    table of name -> name (an empty target means "drop the field"). Both are
    rewrite policy, so a typo here would silently mangle every request the
    model serves — reject it at save time instead.
    """
    efforts = info.get("reasoning_efforts")
    if efforts is not None:
        if not isinstance(efforts, list) or not all(isinstance(v, str) for v in efforts):
            raise HTTPException(
                status_code=400,
                detail=f"{label} '{alias}' field 'reasoning_efforts' must be a list of strings.",
            )
    mapping = info.get("reasoning_effort_map")
    if mapping is not None:
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str))
            for k, v in mapping.items()
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label} '{alias}' field 'reasoning_effort_map' must be a "
                    "table of effort name -> effort name."
                ),
            )


def _validate_overflow(alias: str, info: dict, cloud_types: dict[str, str]) -> None:
    """Peak overflow on an on-prem entry (``config._LOCAL_ROUTING_KEYS``):
    ``overflow_to`` must name a cloud model the chat endpoints can serve,
    on a model they serve too; the server's own waiting thresholds are whole
    numbers, the lower one below the upper one. Empty = unset."""
    target = info.get("overflow_to")
    if target not in (None, ""):
        if not isinstance(target, str):
            raise HTTPException(status_code=400, detail=f"Model '{alias}' field 'overflow_to' must be an alias string.")
        if info.get("type") not in ("llm", "vlm"):
            raise HTTPException(
                status_code=400,
                detail=f"Model '{alias}' cannot overflow: only llm / vlm models are served by the cloud.",
            )
        if target not in cloud_types:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{alias}' overflow_to '{target}' is not an Azure or AWS model.",
            )
        if cloud_types[target] not in ("llm", "vlm"):
            raise HTTPException(
                status_code=400,
                detail=f"Model '{alias}' overflow_to '{target}' has type '{cloud_types[target]}'; it must be llm or vlm.",
            )
    thresholds: dict[str, int] = {}
    for key in ("overflow_waiting_high", "overflow_waiting_low"):
        value = info.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HTTPException(status_code=400, detail=f"Model '{alias}' field '{key}' must be a whole number >= 0.")
        thresholds[key] = value
    if thresholds.get("overflow_waiting_high") == 0:
        raise HTTPException(status_code=400, detail=f"Model '{alias}' field 'overflow_waiting_high' must be at least 1.")
    if len(thresholds) == 2 and thresholds["overflow_waiting_low"] >= thresholds["overflow_waiting_high"]:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{alias}': overflow_waiting_low must be below overflow_waiting_high.",
        )


def _validate_rate_limit_fallback(
    label: str, alias: str, info: dict, models: dict,
) -> None:
    """`rate_limit_fallback` must name another entry of the same backend
    that the chat/messages surfaces can serve (llm/vlm). Empty = unset."""
    target = info.get("rate_limit_fallback")
    if target in (None, ""):
        return
    if not isinstance(target, str):
        raise HTTPException(
            status_code=400,
            detail=f"{label} '{alias}' field 'rate_limit_fallback' must be an alias string.",
        )
    if target == alias:
        raise HTTPException(
            status_code=400,
            detail=f"{label} '{alias}' cannot be its own rate_limit_fallback.",
        )
    target_info = models.get(target)
    if not isinstance(target_info, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{label} '{alias}' rate_limit_fallback references unknown alias '{target}'.",
        )
    if target_info.get("type", "llm") not in ("llm", "vlm"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} '{alias}' rate_limit_fallback '{target}' has type "
                f"'{target_info.get('type')}'; it must be llm or vlm."
            ),
        )


# ── Model Configuration ──

@router.get("/models", response_class=HTMLResponse)
def admin_models_page(
    request: Request,
    admin_user: User = Security(get_web_user, scopes=["admin"]),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "admin_models.html",
        {
            "request": request,
            "app_title": APP_TITLE,
            "title": "Model Configuration",
            "user": admin_user,
            "display_name": admin_user.display_name or admin_user.username,
            "org_code": admin_user.org_code,
            "current_year": datetime.now(timezone.utc).year,
        },
    )


@router.get("/api/config")
def get_config_api():
    # Secrets go out as references (config.masked_config_data), never values.
    return JSONResponse(masked_config_data())


def _clean_display_order(
    raw: object, maps: dict[str, dict | None],
) -> dict[str, list[str]] | None:
    """Validate ``display_order`` ({backend: [alias, ...]}) from a config PUT.

    ``None`` (omitted) leaves the section as it is. Aliases that are not
    models of that backend (renamed or deleted since the page loaded) and
    repeats are dropped rather than refused: the order is display-only.
    A backend whose models the payload omits is checked against the
    current config, which save_config leaves untouched."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="display_order must be an object of alias lists.")
    unknown = sorted(set(raw) - set(DISPLAY_ORDER_BACKENDS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"display_order: unknown backend(s) {unknown}.")
    current = {
        "vllm": get_model_routing_snapshot,
        "azure": get_azure_models_snapshot,
        "bedrock": get_bedrock_models_snapshot,
    }
    out: dict[str, list[str]] = {}
    for backend in DISPLAY_ORDER_BACKENDS:
        aliases = raw.get(backend, [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise HTTPException(status_code=400, detail=f"display_order.{backend} must be a list of aliases.")
        known = maps[backend] if maps[backend] is not None else current[backend]()
        out[backend] = list(dict.fromkeys(a for a in aliases if a in known))
    return out


_TEST_MODEL_TIMEOUT_S = 10.0


@router.post("/api/test-model")
async def test_model_connection(body: dict):
    """Model Config "Test connection" for a local (vLLM) entry, with the
    values in the editor (saved or not): ``{base_url, api_key, real_model,
    type}``. Asks the server for ``GET {base_url}/models`` (System One:
    its ``/health``) with the entry's key and reports whether it answered
    and whether ``real_model`` is among the models it serves. Read-only
    and free: no inference call, nothing billed."""
    import time

    import httpx

    from app.core.server_state import get_health_client
    from app.services.health import _SYSTEMONE_TYPE, _probe_systemone

    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    if not base_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http:// or https://.")
    try:
        api_key = str(resolve_secret(body.get("api_key")) or "").strip()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"API key: {exc}")
    real_model = str(body.get("real_model") or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = get_health_client()
    t0 = time.monotonic()

    def ms() -> float:
        return round((time.monotonic() - t0) * 1000.0, 1)

    if body.get("type") == _SYSTEMONE_TYPE:
        try:
            ok = await _probe_systemone(client, base_url, headers)
        except Exception as exc:
            return JSONResponse({"ok": False, "latency_ms": ms(), "detail": type(exc).__name__})
        return JSONResponse({
            "ok": ok, "latency_ms": ms(),
            "detail": "health check passed" if ok else "health check failed",
        })

    try:
        resp = await client.get(f"{base_url}/models", headers=headers, timeout=_TEST_MODEL_TIMEOUT_S)
    except httpx.TimeoutException:
        return JSONResponse({"ok": False, "latency_ms": ms(), "detail": f"no answer within {_TEST_MODEL_TIMEOUT_S:g}s"})
    except Exception as exc:
        return JSONResponse({"ok": False, "latency_ms": ms(), "detail": f"cannot connect ({type(exc).__name__})"})
    latency = ms()
    if resp.status_code in (401, 403):
        return JSONResponse({"ok": False, "latency_ms": latency, "detail": f"HTTP {resp.status_code}: check the API key"})
    if resp.status_code != 200:
        return JSONResponse({"ok": False, "latency_ms": latency, "detail": f"HTTP {resp.status_code}"})
    served: list[str] = []
    try:
        for item in resp.json().get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                served.append(item["id"])
    except Exception:
        return JSONResponse({"ok": False, "latency_ms": latency, "detail": "/models did not return a model list"})
    found = bool(real_model) and real_model in served
    if not real_model:
        detail = "server reachable; set Real model to check it is served"
    elif found:
        detail = f"server reachable, serves {real_model}"
    else:
        detail = f"server reachable, but {real_model} is not among its models"
    return JSONResponse({
        "ok": found, "latency_ms": latency, "detail": detail, "served_models": served[:20],
    })


@router.put("/api/config")
async def save_config_api(
    request: Request,
):
    body = await request.json()

    models = body.get("models")
    pricing = body.get("pricing")
    fallback = body.get("fallback")
    azure_models = body.get("azure_models")
    azure_fallback = body.get("azure_fallback")
    # Optional: older admin payloads omit these, and save_config leaves the
    # [bedrock_*] sections untouched when they're None.
    bedrock_models = body.get("bedrock_models")
    bedrock_fallback = body.get("bedrock_fallback")

    if not isinstance(models, dict) or not isinstance(pricing, dict):
        raise HTTPException(status_code=400, detail="Invalid config format.")
    if azure_models is not None and not isinstance(azure_models, dict):
        raise HTTPException(status_code=400, detail="Invalid azure_models format.")
    if azure_fallback is not None and not isinstance(azure_fallback, dict):
        raise HTTPException(status_code=400, detail="Invalid azure_fallback format.")
    if bedrock_models is not None and not isinstance(bedrock_models, dict):
        raise HTTPException(status_code=400, detail="Invalid bedrock_models format.")
    if bedrock_fallback is not None and not isinstance(bedrock_fallback, dict):
        raise HTTPException(status_code=400, detail="Invalid bedrock_fallback format.")
    # The editor holds references to stored secrets, not the secrets.
    try:
        resolve_secret_refs(
            {"models": models, "azure_models": azure_models, "bedrock_models": bedrock_models}
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"API key: {exc}")
    display_order = _clean_display_order(
        body.get("display_order"),
        {"vllm": models, "azure": azure_models, "bedrock": bedrock_models},
    )

    # Aliases must be unique across backends — _build_config refuses to load
    # a config that repeats one, and it only runs AFTER save_config has
    # written the file, so catch it here instead of persisting a broken
    # config.toml.
    _seen_alias: dict[str, str] = {}
    for _label, _map in (
        ("vLLM", models), ("Azure", azure_models or {}), ("Bedrock", bedrock_models or {}),
    ):
        for _alias in _map:
            if _alias in _seen_alias:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Alias '{_alias}' is used by both a {_seen_alias[_alias]} "
                        f"and a {_label} model. Aliases must be unique across backends."
                    ),
                )
            _seen_alias[_alias] = _label

    # Validate model entries have required fields + sanity-check metadata
    _META_TYPES: dict[str, type | tuple[type, ...]] = {
        "display_name": str,
        "context_window": int,
        "max_output_tokens": int,
        "supports_tools": bool,
        "supports_vision": bool,
        "supports_prompt_caching": bool,
        "is_reasoning": bool,
    }
    _INTERNAL_TYPES: dict[str, type | tuple[type, ...]] = {
        "hidden": bool,
        "list_in_api": bool,
        "native_messages": bool,
    }

    def _check_internal(label: str, alias: str, info: dict) -> None:
        for key in _MODEL_INTERNAL_KEYS:
            if key in info and not isinstance(info[key], _INTERNAL_TYPES[key]):
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} '{alias}' field '{key}' has wrong type.",
                )
    # What an on-prem model may overflow to: the cloud models as this save
    # leaves them (a section the payload omits stays as stored).
    stored = get_config_data()
    cloud_types: dict[str, str] = {}
    for section, sent in (("azure_models", azure_models), ("bedrock_models", bedrock_models)):
        for cloud_alias, cloud_info in (sent if sent is not None else stored[section]).items():
            if isinstance(cloud_info, dict):
                cloud_types[cloud_alias] = str(cloud_info.get("type") or "llm")
    for alias, info in models.items():
        if not isinstance(info, dict):
            raise HTTPException(status_code=400, detail=f"Invalid model entry: {alias}")
        if not info.get("base_url") or not info.get("type"):
            raise HTTPException(status_code=400, detail=f"Model '{alias}' missing base_url or type.")
        for key in _MODEL_METADATA_KEYS:
            if key not in info:
                continue
            expected = _META_TYPES[key]
            value = info[key]
            # JSON booleans arrive as Python bool which is also an int, so
            # check bool BEFORE int to avoid accepting True as context_window.
            if expected is int and isinstance(value, bool):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be an integer, got boolean.",
                )
            if not isinstance(value, expected):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' has wrong type.",
                )
            if expected is int and value < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be non-negative.",
                )
        _check_internal("Model", alias, info)
        _validate_overflow(alias, info, cloud_types)
        _validate_reasoning_keys("Model", alias, info)
        for key in _MODEL_PRICING_KEYS:
            if key not in info:
                continue
            value = info[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be a number.",
                )
            if value < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Model '{alias}' field '{key}' must be non-negative.",
                )

    # Validate fallback values are strings
    if fallback and not all(isinstance(v, str) for v in fallback.values()):
        raise HTTPException(status_code=400, detail="Fallback values must be strings.")
    if azure_fallback and not all(isinstance(v, str) for v in azure_fallback.values()):
        raise HTTPException(status_code=400, detail="Azure fallback values must be strings.")
    # Each azure_fallback alias must point to an existing Azure deployment
    # of the matching type. Catching the typo here is friendlier than letting
    # _resolve_azure silently ignore the entry at runtime.
    if azure_fallback and azure_models is not None:
        for type_key, alias in azure_fallback.items():
            if not alias:
                continue
            if alias not in azure_models:
                raise HTTPException(
                    status_code=400,
                    detail=f"Azure fallback for type '{type_key}' references unknown alias '{alias}'.",
                )
            entry_type = azure_models[alias].get("type", "llm")
            if entry_type != type_key:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Azure fallback for type '{type_key}' points to '{alias}' "
                        f"which has type '{entry_type}'."
                    ),
                )

    # Validate Bedrock fallback the same way as Azure's above.
    if bedrock_fallback:
        if not all(isinstance(v, str) for v in bedrock_fallback.values()):
            raise HTTPException(status_code=400, detail="Bedrock fallback values must be strings.")
        for type_key, alias in bedrock_fallback.items():
            if not alias:
                continue
            target = (bedrock_models or {}).get(alias)
            if bedrock_models is not None and target is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Bedrock fallback for type '{type_key}' references unknown alias '{alias}'.",
                )
            if target is not None and target.get("type", "llm") != type_key:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Bedrock fallback for type '{type_key}' points to '{alias}' "
                        f"which has type '{target.get('type', 'llm')}'."
                    ),
                )

    # Validate Azure + Bedrock model entries (same metadata / pricing /
    # reasoning / rate-limit rules; only the connection fields differ).
    for backend, cloud_models, required_fields in (
        ("Azure", azure_models, ("type", "endpoint", "deployment", "api_key")),
        ("Bedrock", bedrock_models, ("type", "model_id")),
    ):
        if not cloud_models:
            continue
        for alias, info in cloud_models.items():
            if not isinstance(info, dict):
                raise HTTPException(status_code=400, detail=f"Invalid {backend.lower()} model entry: {alias}")
            for required in required_fields:
                if not info.get(required):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' missing required field '{required}'.",
                    )
            if backend == "Bedrock":
                has_key_id = bool(info.get("aws_access_key_id"))
                has_secret = bool(info.get("aws_secret_access_key"))
                if has_key_id != has_secret:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Bedrock model '{alias}' needs both aws_access_key_id and aws_secret_access_key.",
                    )
                shared = bedrock_shared_settings()
                shared_iam = bool(shared["aws_access_key_id"] and shared["aws_secret_access_key"])
                if not (info.get("api_key") or has_key_id or shared_iam):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Bedrock model '{alias}' needs an api_key or aws_access_key_id + "
                            "aws_secret_access_key (or BEDROCK_AWS_ACCESS_KEY_ID / "
                            "BEDROCK_AWS_SECRET_ACCESS_KEY in .env)."
                        ),
                    )
            for key in _MODEL_METADATA_KEYS:
                if key not in info:
                    continue
                expected = _META_TYPES[key]
                value = info[key]
                if expected is int and isinstance(value, bool):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be an integer, got boolean.",
                    )
                if not isinstance(value, expected):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' has wrong type.",
                    )
                if expected is int and value < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be non-negative.",
                    )
            _validate_reasoning_keys(f"{backend} model", alias, info)
            _validate_rate_limit_fallback(f"{backend} model", alias, info, cloud_models)
            _check_internal(f"{backend} model", alias, info)
            # Bedrock's Anthropic endpoint only serves Claude; the proxy
            # would ignore the flag on any other model.
            if (
                backend == "Bedrock" and info.get("native_messages")
                and "anthropic" not in str(info.get("model_id") or "").lower()
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Bedrock model '{alias}': native_messages needs a Claude model_id "
                        "(one containing 'anthropic')."
                    ),
                )
            for key in _MODEL_PRICING_KEYS:
                if key not in info:
                    continue
                value = info[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be a number.",
                    )
                if value < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{backend} model '{alias}' field '{key}' must be non-negative.",
                    )

    # Audit trail: which aliases and fields changed (names only).
    audit.note(config_changes=audit.config_diff(
        get_config_data(),
        {
            "models": models, "pricing": pricing, "fallback": fallback or {},
            "azure_models": azure_models, "azure_fallback": azure_fallback,
            "bedrock_models": bedrock_models, "bedrock_fallback": bedrock_fallback,
            "display_order": display_order,
        },
    ))
    save_config(
        models, pricing, fallback or {}, azure_models, azure_fallback,
        bedrock_models, bedrock_fallback, display_order,
    )
    return JSONResponse({"ok": True})
