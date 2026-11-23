"""
The account endpoints: what an account reads and reports about itself, by
API key.

``/v1`` is two different things that share a prefix, and they are kept in
two modules so they are not mixed up:

- ``v1_api.py``, the **model API**: inference. Billed, limited, routed to a
  backend.
- this one, the **account endpoints**: ``GET /v1/me`` (also ``/me``),
  ``/v1/status``, ``/v1/usage``, ``/v1/service-accounts``, ``/v1/notices``,
  ``/v1/settings``, and the two that write, ``POST /v1/notices/resolved``
  and ``POST /v1/events``. What the dashboard shows an account, as JSON,
  for a program that is not a browser (a terminal tool, a status line).
  Never billed, never routed to a model; they answer once the budget is
  spent, since that is when people look.

Every route here authenticates with ``get_current_user_unmetered`` (or
``get_account_holder``, which also refuses a request a service account
sends for a user), so it passes the IP allowlist like every API route.
None returns an API key.

The five reads the dashboard's own API answers too (``me``, ``status``,
``usage``, ``service-accounts``, ``notices``) have their body built in
``app/services/account_view.py``: a handler here authenticates and calls
it. These answers are what programs outside this repository are built
against: change them by addition only, and never return a ``null``.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.core.deps import (
    get_account_holder,
    get_current_user_unmetered,
    record_client_event,
    resolve_client_notices,
)
from app.core.logger import logger
from app.models.schema import User
from app.services import account_view, audit, client_events, client_settings

# One group of /docs: every route here is an account endpoint.
router = APIRouter(tags=["account"])


@router.get("/v1/me")
@router.get("/me")
async def account_info(user: User = Depends(get_current_user_unmetered)):
    """The caller's own access and today's budget, for clients and scripts.

    Answers even once the daily limit is spent (no 429 at auth), since
    "how much is left / when does it reset" is what a client asks then.
    Every account has a finite limit, so every amount is a number (never
    null, no "unlimited").

    Per backend (``onprem`` = vLLM, ``azure``, ``aws`` = Bedrock):
      - ``access``: may this key reach it (the account's Azure / AWS flag);
      - ``used_usd``: today's spend there;
      - ``remaining_usd``: what can still be spent there today — the tighter
        of the cloud cap (azure / aws) and the overall daily limit (cloud
        spend counts toward both), 0 without access.

    ``cloud`` appears only when the caller may use a cloud backend and has a
    cloud cap (``effective_cloud_limit``: their own, else the gateway-wide
    default): Azure + AWS spend today against it.

    Limits are those the gateway enforces; with ``ENFORCE_DAILY_LIMIT=false``
    they are only logged (``daily.enforced`` = false).

    ``legacy_key``: the key is of the older, weaker format and the dashboard
    asks its holder to regenerate it. The key itself is never returned.
    """
    return await run_in_threadpool(account_view.me, user)


@router.get("/v1/status")
async def model_status(user: User = Depends(get_current_user_unmetered)):
    """The models this key can use and whether they are up, for clients and
    scripts: what the dashboard's model tables show.

    ``models`` lists every model the account can see (hidden entries and
    cloud backends without access left out), in the dashboard's order, each
    with its billed prices per 1M tokens. On-prem models carry ``state``
    ``up`` / ``down`` from the gateway's health loop, and ``running`` /
    ``waiting`` request counts when the server reports them; cloud models
    are ``unchecked`` (the gateway does not probe them). ``onprem`` sums
    the on-prem ones up. A key is never omitted as null: it is left out.

    Like ``/v1/me`` it answers once the daily limit is spent. No database
    read.
    """
    return account_view.status(user)


@router.get("/v1/usage")
def usage_history(
    days: int = Query(7, ge=1, le=account_view.USAGE_MAX_DAYS, description="How many days back, today included."),
    user: User = Depends(get_account_holder),
):
    """The caller's own usage: one row per day and this month by model,
    the figures behind the dashboard's trend chart and Cost by Model card.

    ``days`` are Asia/Taipei calendar days, oldest first, today last and
    still running; quiet days are present as zeros. ``month`` is the local
    month so far, with one ``models`` row per model and backend in the
    dashboard's order. Like the dashboard, the month leaves out a cloud
    backend the account no longer has access to; the days count everything
    (as the daily budget does). A row with ``overflow`` true is a cloud
    model the gateway served in place of a congested on-prem one: it is
    listed whatever cloud access the account has, and is on its bill.

    ``sources``, in ``today`` and in ``month``, is that same usage by the
    way it came in: ``source`` ``own_key`` for requests sent with the
    account's own key, and one ``service_account`` entry per application
    that sent requests for the account after its user signed in to it
    (``X-On-Behalf-Of``), named in ``service_account`` and, when an admin
    described it, ``description``. Each has its totals and its own
    ``models`` rows; together they add up to the period's totals. It is
    part of the figures above, never on top of them. Other ``source``
    values may be added later: skip one you do not know.

    Answers once the daily limit is spent. Plain ``def``: it reads the
    database and awaits nothing.
    """
    return account_view.usage(user, days)


@router.get("/v1/service-accounts")
def owned_service_accounts(user: User = Depends(get_account_holder)):
    """The service accounts the caller owns (accounts for programs, assigned
    by an admin), with each one's daily limit and this month's usage, in
    total and per model: the dashboard's *Service accounts* tab.

    ``month.models`` has one row per model and backend, in the dashboard's
    order (as ``month.models`` of ``/v1/usage``), and adds up to the
    month's totals. Nothing is left out of it: it is that account's bill,
    whatever cloud access the caller has.

    ``description`` is one line an admin wrote to say what the account is,
    ``department`` the department it is for; each key is absent when there
    is none. An account made for another department than the caller's is
    left out of the list, as on the dashboard, until an admin has looked.

    ``acts_for_users`` says the account sends requests for the people using
    it (``X-On-Behalf-Of``); ``on_behalf`` is what it sent for them this
    month and for how many ``users``. That usage is billed to those users,
    so it is no part of ``month``.

    A service account's API key is **not** part of it, and no API-key
    endpoint returns one: a leaked personal key must not open every service
    account its holder owns. Showing and regenerating such a key stays on
    the dashboard, behind SSO. ``legacy_key`` says its key is of the older
    format.

    Answers once the daily limit is spent. Plain ``def`` (database read).
    """
    return account_view.service_accounts(user)


@router.get("/v1/notices")
def client_notices(user: User = Depends(get_account_holder)):
    """What the dashboard's notices panel tells the caller: the client
    checks (``CC001``...) this account tripped in the last ``window_days``
    days and has not reported as fixed.

    Each notice has the check's ``id``, the admin's ``title`` and
    ``message``, when it was last seen and, when the fix is a setting, its
    ``patch``. The top-level ``patch`` is the account's one patch for
    everything open (what the dashboard offers as a download): ``covers``
    lists the checks it fixes and ``manual`` those no setting can fix
    (update Claude Code, ask for access). It is absent when nothing is
    open. A patch only ever sets or removes variables in the ``env`` block
    of settings.json, and never holds a key.

    ``model_changes`` is a block of its own beside them: the models an admin
    is replacing (admin panel, *Model changes*) and is showing now, for this
    account. It is a second category of notice, not a violation: nothing was
    detected, the account is simply one the change is for. Nothing of it is
    in ``notices`` or in the top-level ``patch``, its patch is renames and
    never ``set`` / ``unset``, and ``POST /v1/notices/resolved`` does not
    touch it. A client that reads only ``notices`` and ``patch`` sees what
    it always saw.

    In it, ``changes`` lists each change being shown that concerns the
    account's tier (its cloud access: none, Azure, AWS or both): its ``id``
    (``MC3``), ``title`` and ``message``, the ``models`` to replace
    (``{old: new}``, this tier's) and whether the account said it switched
    on its dashboard (``applied``). ``retiring`` is every model name being
    replaced for this account, and ``patch.models`` the one rename map for
    all of it (a later change wins where two replace the same model). All
    three are empty unless an admin has a change switched on that is for
    the caller.

    Applying ``patch.models``: replace the old name with the new one where
    Claude Code's settings.json names a model (the ``model`` setting and
    the ``env`` variables with ``MODEL`` in their name), only where the
    value is exactly the old name; a bracket suffix (``old[1m]``) is kept.
    Add nothing: a user who chose another model keeps it.

    Read-only: reporting a notice as fixed is ``POST /v1/notices/resolved``.
    Answers once the daily limit is spent. Plain ``def``.
    """
    return account_view.notices(user)


@router.post("/v1/notices/resolved")
def client_notices_resolved(
    body: dict | None = Body(default=None),
    user: User = Depends(get_account_holder),
):
    """*I've applied it*, for a client that is not a browser: the caller's
    open notices are marked as fixed, as the dashboard's button does.

    Without a body (or without ``ids``) everything open is resolved; with
    ``{"ids": ["CC001", ...]}`` only those. The answer lists what was open
    and is now resolved (``resolved``, in the checks' order), so a repeated
    call answers an empty list and an id with nothing open is not an error.
    The gateway cannot see a settings file and takes the account's word
    for two weeks (``client_rules.RESOLVED_QUIET_DAYS``): a notice comes
    back only if its problem is still seen after that.

    The one write an API key has on its account, and in the audit log.
    Plain ``def``.
    """
    ids = None
    if body is not None and body.get("ids") is not None:
        ids = body["ids"]
        if (
            not isinstance(ids, list) or len(ids) > 50
            or not all(isinstance(rule_id, str) and len(rule_id) <= 20 for rule_id in ids)
        ):
            raise HTTPException(status_code=400, detail="ids must be a list of notice ids, such as [\"CC001\"].")
    audit.note_actor(user.username)
    try:
        resolved = resolve_client_notices(user, ids)
    except Exception as exc:
        logger.warning("Client notices resolve failed | user={} error={}: {}", user.username, type(exc).__name__, exc)
        raise HTTPException(status_code=503, detail="Could not record this now. Try again later.")
    audit.note(resolved=resolved)
    logger.info("Client rules marked as fixed by API key | user={} rules={}", user.username, ",".join(resolved) or "-")
    return {"object": "notices.resolved", "resolved": resolved}


@router.post("/v1/events")
def client_event(
    body: dict = Body(...),
    user: User = Depends(get_account_holder),
):
    """What a setup tool did on its user's machine, for the admin's *Client
    installs* list: which account installed, updated, removed or configured
    what, in which version, on which operating system.

    One event per request: ``event`` (``installed`` / ``updated`` /
    ``uninstalled`` / ``settings_applied``), the reporting ``tool`` and its
    ``tool_version``, the ``component`` it is about (``claude-code``,
    ``vscode``, a skill, an MCP server, ...) and ``os`` (``windows`` /
    ``linux`` / ``macos``) are required; ``kind`` (``app``, the default, or
    ``skill`` / ``mcp`` / ``plugin``, with the program it was added to in
    ``client``),
    ``component_version``, ``previous_version``, ``method``, ``os_version``,
    ``arch``, ``hostname`` (the machine's name: what tells two machines of
    one account apart), ``ok`` (false: the attempt failed) and ``message``
    are optional. Keys the gateway does not know are ignored.

    Telemetry and nothing else: it changes nothing about the account, and
    nothing routes, limits or refuses by what was reported. A tool sends it
    and carries on whatever the answer: 400 for an event that does not
    validate, 429 past the hourly allowance, 503 when it could not be
    kept. Never billed; answers once the daily limit is spent. Plain ``def``.
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


@router.get("/v1/settings")
async def client_settings_fields(user: User = Depends(get_account_holder)):
    """The client settings this account can be offered, for a tool that
    sets a client up: apply every ``default``, or let the user pick.

    A field is one variable of the ``env`` block of Claude Code's
    settings.json (``client`` names the client): ``name``, a ``label`` and
    a ``description`` to show, ``type`` (``model``: its options are models;
    ``choice``: fixed values), ``default`` and ``options``. ``default`` is
    never empty and is always the ``value`` of one of the ``options``; a
    field with one option has nothing else to pick. An option has a
    ``value`` and a ``label``, sometimes a ``description``, and a model
    option its ``backend``.

    The fields are the admin's (``client_settings.json``, the admin panel's
    *Client setup* card), and both ``default`` and ``options`` follow the
    account's tier, its cloud access: none, Azure, AWS or both. A model the
    account cannot use is never offered. What the account has set now is on
    its machine; the gateway does not know it.

    Read-only and never a key. Answers once the daily limit is spent. No
    database read.
    """
    return {
        "object": "settings",
        "client": client_settings.CLIENT,
        "fields": client_settings.fields_for(user),
    }
