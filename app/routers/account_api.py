"""
The account endpoints: what an account reads and reports about itself, by
API key or by SSO sign-in.

``/v1`` is two different things that share a prefix, and they are kept in
two modules so they are not mixed up:

- ``v1_api.py``, the **model API**: inference. Billed, limited, routed to a
  backend. API key only.
- this one, the **account endpoints**: ``GET /v1/me`` (also ``/me``),
  ``/v1/status``, ``/v1/usage``, ``/v1/service-accounts``, ``/v1/notices``,
  ``/v1/settings``, and the two that write, ``POST /v1/notices/resolved``
  and ``POST /v1/events``. What the dashboard shows an account, as JSON.
  Never billed, never routed to a model; they answer once the budget is
  spent, since that is when people look.

Two kinds of client ask, and one route serves both
(``deps.get_account_user``):

- a program that is not a browser (a terminal tool, a status line), with
  the account's **API key**. It passes the IP allowlist like every API
  route.
- a browser, with the person's **SSO sign-in**: a frontend of its own for
  the dashboard. The proxy in front trades the session cookie for the
  access token, so it arrives as the same ``Authorization: Bearer`` header.
  The web UI's rules apply: the token check and account of
  ``get_web_user``, a write refused cross-site, no IP allowlist.

Every route authenticates with ``get_account_user``, and answers about the
account the credential belongs to, nobody else's: ``X-On-Behalf-Of`` (a
service account sending model requests for its users) is refused here.
Both sign-ins get the same answer, with two exceptions for a person signed
in by SSO (``account_view.*_signed_in``): ``is_admin``, and who a service
account sent requests for. None returns an API key in full, to either:
showing and regenerating a key stays under ``/dashboard``, behind the
proxy's sign-in.

Each body is built in ``app/services/account_view.py``: a handler here
authenticates and calls it. These answers are what programs outside this
repository are built against: change them by addition only, and never
return a ``null``.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import api_base_url
from app.core.deps import (
    get_account_user,
    record_client_event,
    resolve_client_notices,
)
from app.core.logger import logger
from app.models.schema import User, signed_in_by_sso
from app.services import account_view, audit, client_events, client_settings

# What every route here can answer besides its own answer, said once: the
# group's description in /docs carries the same three.
_COMMON_ERRORS: dict[int | str, dict] = {
    401: {"description": "No credential, or one the gateway does not know: an invalid API key, or an access "
                         "token that is expired or was not issued for this gateway."},
    403: {"description": "The account is disabled; the API key comes from an address the IP allowlist refuses; "
                         "the request carries `X-On-Behalf-Of` (these routes answer about the credential's own "
                         "account); the access token lacks the `read` scope; or a write signed in by SSO came "
                         "from another site's page."},
    429: {"description": "This address sent too many different invalid API keys and is locked for a while "
                         "(`Retry-After`, seconds). Never the daily budget: these routes answer once it is spent."},
}

# One group of /docs: every route here is an account endpoint.
router = APIRouter(tags=["account"], responses=_COMMON_ERRORS)

# Every docstring below is the route's description in /docs and openapi.json,
# and they are written to one plan, in this order, so a reader finds the same
# thing in the same place: a first line that says what the route answers;
# **Sign-in**; **Query** / **Body** (when it takes any); **Response** (a table,
# one row per key); **Only for an SSO sign-in** (when something is); **Limits
# and things to know** (ranges, how fresh a figure is, what is left out);
# **Errors** (the route's own; 401, 403 and 429 are the group's, above).


@router.get("/v1/me", summary="Account and today's budget")
@router.get("/me", summary="Account and today's budget (alias of /v1/me)")
async def account_info(request: Request, user: User = Depends(get_account_user)):
    """Who the caller is, what they may reach, and how much of today's budget is left.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `account` |
    | `username`, `display_name` | string | The account |
    | `org_code` | string | The person's department. Absent when their sign-in gave none |
    | `legacy_key` | bool | The API key is of the older, weaker format: regenerate it on the dashboard |
    | `api_key.masked` | string | The key's start and last four characters: enough to recognise it, not to use it |
    | `api_key.created_at` | datetime | When the key was issued. Absent for a key older than this record |
    | `daily.limit_usd`, `used_usd`, `remaining_usd` | number | Today's budget over every backend |
    | `daily.requests`, `input_tokens`, `output_tokens` | integer | Today's totals |
    | `daily.enforced` | bool | `false`: the gateway only logs an overage instead of answering 429 |
    | `daily.timezone`, `resets_at`, `resets_in_seconds` | string, datetime, integer | When the day ends |
    | `backends.onprem` / `azure` / `aws` | object | `access` (bool), `used_usd`, `remaining_usd` there today |
    | `cloud` | object | `limit_usd`, `used_usd`, `remaining_usd` for Azure and AWS together. Absent without cloud access or without a cloud cap |
    | `cloud_budget_fallback` | bool | `true`: a cloud model whose cap is spent is answered by an on-prem model instead of a 429 |
    | `concurrency` | object | `mode` (`off` / `monitor` / `enforce`); with a limit also `limit` (on-prem requests in flight), `cloud_limit` (with cloud access) and `offpeak` (`start`, `end`, `weekends`, `active`) when off-peak hours are set |
    | `site` | object | `title`, `api_base` (the URL to print in examples), `links` (the admin's, only the ones set), `setup_page`, `installer` (bool) |

    **Only for an SSO sign-in**

    | Key | Type | Meaning |
    |---|---|---|
    | `is_admin` | bool | The access token has the `admin` scope. Absent by API key: a key carries no admin right and cannot say |

    **Limits and things to know**

    - Every amount is a number: there is no "unlimited". A limit of `0` is a $0 budget.
    - A day is the `Asia/Taipei` calendar day.
    - The `remaining_usd` of the backends share one budget and do not add up: each is the tighter of the
      cloud cap (Azure / AWS) and the overall remainder, and `0` without access.
    - A request is refused only once `used_usd` has reached the limit, so the day's last request can
      overshoot it slightly.
    - Answers when the budget is spent, and is never billed.
    - No value is ever `null`: a key without a value is left out.

    **Errors.** Only the group's (401, 403, 429).
    """
    body = await run_in_threadpool(account_view.me, user)
    body.update(account_view.me_holder(user, api_base_url(request)))
    if signed_in_by_sso(user):
        body.update(account_view.me_signed_in(user))
    return body


@router.get("/v1/status", summary="Models and whether they are up")
async def model_status(user: User = Depends(get_account_user)):
    """The models the account can see, each with its state, load and price: the dashboard's model tables.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `status` |
    | `onprem.total`, `onprem.up` | integer | How many on-prem models there are, and how many are up |
    | `onprem.down` | array of string | The ids of the on-prem models that are down |
    | `models` | array | One entry per model, on-prem first, then Azure, then AWS, in the dashboard's order |
    | `models[].id` | string | The name to send as `model` |
    | `models[].backend` | string | `onprem`, `azure` or `aws` |
    | `models[].type` | string | `llm`, `vlm`, `embedding`, `reranker`, ... |
    | `models[].state` | string | `up` / `down` (on-prem), `unchecked` (cloud: the gateway does not probe it) |
    | `models[].falls_back_to` | string | On a `down` model: the model that answers in its place. Absent when none can |
    | `models[].running`, `waiting` | integer | Requests the server is running and has queued. Absent when it does not report them |
    | `models[].input_price_per_1m`, `output_price_per_1m` | number | The billed price per 1M tokens |
    | `models[].display_name`, `context_window`, `max_output_tokens`, `supports_*`, `is_reasoning`, ... | various | What the admin declared for the model. Each absent when not declared |

    **Limits and things to know**

    - `state` and the load come from the gateway's health check, which runs every 30 seconds and marks a
      server down after two failures in a row: a figure can be that old.
    - A model the admin hid from the dashboard is not listed, and neither is a cloud backend the account
      has no access to. A model that is not listed is still served when a request names it.
    - Signed in by SSO with the `admin` scope, the cloud models are listed whatever the account's own access.
    - No database read. Answers when the budget is spent, and is never billed.

    **Errors.** Only the group's (401, 403, 429).
    """
    return account_view.status(user)


@router.get("/v1/usage", summary="Usage per day, and by model")
def usage_history(
    days: int = Query(7, ge=1, le=account_view.USAGE_MAX_DAYS, description="How many days back, today included."),
    user: User = Depends(get_account_user),
):
    """The account's own usage: one row per day, and today and this month by model and by the way it came in.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Query**

    | Name | Type | Default | Limits |
    |---|---|---|---|
    | `days` | integer | `7` | `1` to `90`. Today counts as one |

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object`, `timezone` | string | `usage`, `Asia/Taipei` |
    | `days` | array | Oldest first, today last. `date`, `requests`, `input_tokens`, `output_tokens`, `cost_usd` |
    | `today`, `month` | object | `date` / `month`, the period's `requests`, `input_tokens`, `output_tokens`, `cost_usd`, then `models` and `sources` |
    | `models[]` | object | `model`, `backend`, `type`, `requests`, `input_tokens`, `output_tokens`, `cost_usd`, `overflow` |
    | `models[].overflow` | bool | `true`: a cloud model the gateway served in place of a congested on-prem one. It is on the bill |
    | `sources[]` | object | The same usage by the way it came in: `source`, its totals and its own `models` |
    | `sources[].source` | string | `own_key`, or `service_account` (an application that sent requests for this account) |
    | `sources[].service_account`, `description` | string | That application's name, and what an admin wrote about it. Absent for `own_key` / when not written |

    **Limits and things to know**

    - Days and the month are `Asia/Taipei` calendar days and months. Today is still running.
    - A quiet day is present as zeros, so `days` always has exactly as many entries as asked for.
    - `sources` is a split of the period's totals, never an amount beside them: its entries add up to them.
    - `today` and `month` leave out a cloud backend the account no longer has access to, as the dashboard
      does (`overflow` rows excepted); `days` counts everything, as the daily budget does.
    - Skip a `source` value you do not know: others may be added.
    - Answers when the budget is spent, and is never billed.

    **Errors**

    | Status | When |
    |---|---|
    | 422 | `days` is not an integer from 1 to 90 |
    """
    return account_view.usage(user, days)


@router.get("/v1/service-accounts", summary="The service accounts the caller owns")
def owned_service_accounts(user: User = Depends(get_account_user)):
    """The service accounts (accounts for programs) an admin assigned to the caller, with this month's usage.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object`, `month` | string | `service_accounts`, the month the figures are for (`2026-11`) |
    | `service_accounts` | array | By name. Empty when the caller owns none |
    | `[].id`, `username` | integer, string | The account |
    | `[].description`, `department` | string | What an admin wrote about it, and the department it is for. Each absent when there is none |
    | `[].daily_limit_usd` | number | Its own daily limit |
    | `[].api_key_masked`, `legacy_key` | string, bool | Its key, masked, and whether it is of the older format |
    | `[].month` | object | `requests`, `input_tokens`, `output_tokens`, `cost_usd`, and `models` (one row per model and backend, adding up to the totals) |
    | `[].acts_for_users` | bool | It sends model requests for the people using it (`X-On-Behalf-Of`) |
    | `[].on_behalf` | object | What it sent for them this month: `users`, `requests`, `input_tokens`, `output_tokens`, `cost_usd`. Billed to those users, so no part of `month` |
    | `withheld` | array | Accounts assigned to the caller that are not shown now: `username`, `department` |

    **Only for an SSO sign-in**

    | Key | Type | Meaning |
    |---|---|---|
    | `[].sent_for` | object | Who it sent requests for this month: `users` (how many), and `rows` of one user on one backend with `requests`, tokens and `cost_usd`. Absent when it sent none |
    | `top_users` | integer | How many users `sent_for` names (the ones with the highest cost); the rest are one row per backend with `others` instead of `user` |

    **Limits and things to know**

    - No key in full, to either sign-in: a leaked personal key must not open the service accounts behind
      it. A key is shown and regenerated on the dashboard only.
    - `sent_for` is not given to an API key: a leaked key must not say who uses an application.
    - An account made for another department than the caller's is left out of `service_accounts` and
      named in `withheld` until the departments match again.
    - The figures are the current `Asia/Taipei` month. `month.models` lists every backend the service
      account used, whatever the caller's own cloud access.
    - Answers when the budget is spent, and is never billed.

    **Errors.** Only the group's (401, 403, 429).
    """
    if signed_in_by_sso(user):
        return account_view.service_accounts_signed_in(user)
    return account_view.service_accounts(user)


@router.get("/v1/notices", summary="Client notices and model changes")
def client_notices(user: User = Depends(get_account_user)):
    """What the dashboard tells the caller about their client setup, and the models being replaced for them.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `notices` |
    | `window_days` | integer | A problem counts for this many days after it was last seen (15) |
    | `quiet_days` | integer | A notice reported as applied is left alone for this many days (14) |
    | `notices` | array | The open notices. Empty when nothing is open |
    | `notices[].id` | string | The check, such as `CC001` |
    | `notices[].title`, `message` | string | The admin's wording. `message` absent when empty |
    | `notices[].last_seen_at` | datetime | When the gateway last saw the problem |
    | `notices[].client_version` | string | The client version it was seen with. Absent when unknown |
    | `notices[].patch` | object | Its fix: `set` (variable → value) and `unset` (names). Absent when no setting fixes it |
    | `patch` | object | One fix for everything open: `set`, `unset`, `covers` (the ids it fixes), `manual` (the ids no setting can fix). Absent when nothing is open |
    | `model_changes.changes` | array | The model changes shown to this account: `id` (`MC3`), `title`, `message`, `models` (old → new), `applied` |
    | `model_changes.retiring` | array of string | Every model name being replaced for this account |
    | `model_changes.patch.models` | object | One rename map for all of them (old → new) |

    **Limits and things to know**

    - A patch only sets or removes variables in the `env` block of Claude Code's settings.json. It never
      holds a key.
    - The gateway records a problem at most once an hour per account, so a notice can appear up to an hour
      after the request that caused it.
    - `model_changes` is a second category, not a violation: nothing of it is in `notices` or in the
      top-level `patch`, and `POST /v1/notices/resolved` does not touch it. Its three keys are always
      there, and empty unless an admin is showing a change that is for this account's tier.
    - Applying `model_changes.patch.models`: replace a name only where the value is exactly the old one
      (the `model` setting, and the `env` variables with `MODEL` in their name), keep a bracket suffix
      (`old[1m]`), and add nothing: a user who chose another model keeps it.
    - Read-only. Answers when the budget is spent, and is never billed.

    **Errors.** Only the group's (401, 403, 429).
    """
    return account_view.notices(user)


@router.post(
    "/v1/notices/resolved",
    summary="Report notices as applied",
    responses={
        400: {"description": "`ids` is not a list of at most 50 notice ids of at most 20 characters each."},
        503: {"description": "It could not be recorded now. Nothing was changed: try again later."},
    },
)
def client_notices_resolved(
    body: dict | None = Body(default=None),
    user: User = Depends(get_account_user),
):
    """*I've applied it*: marks the caller's open notices as fixed, as the dashboard's button does.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Body** (optional)

    | Key | Type | Limits | Meaning |
    |---|---|---|---|
    | `ids` | array of string | At most 50, each at most 20 characters | The notices to resolve, such as `["CC001"]`. Without it (or without a body) everything open is resolved |

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `notices.resolved` |
    | `resolved` | array of string | The ids that were open and are now resolved, in the checks' order |

    **Limits and things to know**

    - An id with nothing open is not an error: it is left out of `resolved`, and a repeated call answers
      an empty list.
    - The gateway cannot see a settings file and takes the account's word for `quiet_days` (14): a notice
      comes back only if its problem is still seen after that.
    - It resolves client notices only, never a model change.
    - Every call by an identified account is in the audit log, with how the caller signed in.
    - Signed in by SSO, a call sent by another site's page is refused (403).

    **Errors**

    | Status | When |
    |---|---|
    | 400 | `ids` is not a list of at most 50 ids of at most 20 characters each |
    | 503 | It could not be recorded now; nothing was changed |
    """
    ids = None
    if body is not None and body.get("ids") is not None:
        ids = body["ids"]
        if (
            not isinstance(ids, list) or len(ids) > 50
            or not all(isinstance(rule_id, str) and len(rule_id) <= 20 for rule_id in ids)
        ):
            raise HTTPException(status_code=400, detail="ids must be a list of notice ids, such as [\"CC001\"].")
    via = "sso" if signed_in_by_sso(user) else "api_key"
    audit.note_actor(user.username, via=via)
    try:
        resolved = resolve_client_notices(user, ids)
    except Exception as exc:
        logger.warning("Client notices resolve failed | user={} error={}: {}", user.username, type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail="Could not record this now. Try again later.")
    audit.note(resolved=resolved)
    logger.info(
        "Client rules marked as fixed | user={} via={} rules={}", user.username, via, ",".join(resolved) or "-",
    )
    return {"object": "notices.resolved", "resolved": resolved}


@router.post(
    "/v1/events",
    summary="Report what a setup tool did",
    responses={
        400: {"description": "The event does not validate: the reply says which field."},
        429: {"description": f"More than {client_events.MAX_PER_HOUR} events from this account in the last hour "
                             "(`Retry-After: 3600`); or the address is locked after too many invalid API keys."},
        503: {"description": "The event could not be kept now."},
    },
)
def client_event(
    body: dict = Body(...),
    user: User = Depends(get_account_user),
):
    """What a setup tool installed, updated, removed or configured on its user's machine, for the admin's *Client installs* list.

    **Sign-in.** API key or SSO access token. Recorded under the credential's own account.

    **Body.** One event per request.

    | Key | Required | Limits | Meaning |
    |---|---|---|---|
    | `event` | yes | `installed`, `updated`, `uninstalled`, `settings_applied` | What happened |
    | `tool`, `tool_version` | yes | Name: lower-case letters, digits, `.`, `_`, `-`, at most 40. Version: at most 40 | The reporting tool |
    | `component` | yes | Lower-case letters, digits, `@ . _ / : + -`, at most 100 | What it is about: `claude-code`, `vscode`, `@scope/server-github`, ... |
    | `os` | yes | `windows`, `linux`, `macos` | The operating system |
    | `kind` | no | `app` (default), `skill`, `mcp`, `plugin` | What sort of thing `component` is |
    | `client` | no | A name, at most 40 | The program a skill, MCP server or plugin was added to |
    | `component_version`, `previous_version` | no | At most 40 each | The version now, and the one before an update |
    | `method`, `arch` | no | A name, at most 40 each | How it was installed; the CPU architecture |
    | `os_version` | no | At most 80 characters | Free text |
    | `hostname` | no | Letters, digits, `.`, `_`, `-`, at most 100 | The machine's name: what tells two machines of one account apart |
    | `ok` | no | `true` (default) or `false` | `false`: the attempt failed |
    | `message` | no | At most 500 characters | Free text, such as the error |

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `event` |
    | `recorded` | bool | `true` |

    **Limits and things to know**

    - At most 300 events an hour per account.
    - Keys the gateway does not know are ignored, so a newer tool can say more than an older gateway keeps.
    - The caller's API key is refused in a name or a version, and taken out of `os_version` and `message`.
      Text longer than its limit is cut, not refused.
    - Without `hostname`, an account's machines of one operating system count as one.
    - Telemetry and nothing else: it changes nothing about the account, and nothing routes, limits or
      refuses by it. Send it and carry on whatever the answer.
    - Answers when the budget is spent, and is never billed. Not in the audit log.

    **Errors**

    | Status | When |
    |---|---|
    | 400 | The event does not validate; `detail` says which field |
    | 429 | More than 300 events in the last hour (`Retry-After: 3600`) |
    | 503 | It could not be kept now |
    """
    try:
        values = client_events.clean(body, secret=user.api_key or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        record_client_event(user, values)
    except client_events.TooManyEvents:
        raise HTTPException(
            status_code=429, headers={"Retry-After": "3600"},
            detail=f"At most {client_events.MAX_PER_HOUR} events an hour per account.",
        )
    except Exception as exc:
        logger.warning("Client event not recorded | user={} error={}: {}", user.username, type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail="Could not record this now.")
    return {"object": "event", "recorded": True}


@router.get("/v1/settings", summary="Recommended client settings")
async def client_settings_fields(user: User = Depends(get_account_user)):
    """The client settings a tool can offer this account when it sets a client up: apply every `default`, or let the user pick.

    **Sign-in.** API key or SSO access token. Always the credential's own account.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `object` | string | `settings` |
    | `client` | string | The client the fields are for (`claude-code`) |
    | `fields` | array | One entry per variable of the `env` block of the client's settings.json |
    | `fields[].name` | string | The variable |
    | `fields[].label`, `description` | string | What to show the user |
    | `fields[].type` | string | `model` (its options are models) or `choice` (fixed values) |
    | `fields[].default` | string | The value to apply. Never empty, and always the `value` of one of the `options` |
    | `fields[].options` | array | `value`, `label`, sometimes `description`; a model option also has its `backend` |

    **Limits and things to know**

    - The fields are the admin's (the admin panel's *Client setup* card), so the list can be empty and
      changes without a gateway release.
    - `default` and `options` follow the account's cloud access (none, Azure, AWS or both). A model the
      account cannot use is never offered.
    - A field with one option has nothing else to pick.
    - The gateway does not know what the machine has set now.
    - It never contains a key, and the variables that hold the key are never fields.
    - This is not the fix for a notice: `GET /v1/notices` gives that. A tool that wants both applies these
      defaults first and the notices' patch on top.
    - No database read. Answers when the budget is spent, and is never billed.

    **Errors.** Only the group's (401, 403, 429).
    """
    return {
        "object": "settings",
        "client": client_settings.CLIENT,
        "fields": client_settings.fields_for(user),
    }
