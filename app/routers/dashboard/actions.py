"""
What an account does from its dashboard, and what it downloads there:
regenerate a key (its own, or a service account's it owns), report a notice
or a model change as applied, and the scripts (the notices' patch, the
model-change renames, the API key, the Windows installer).

Every write answers JSON to a caller that sends ``Accept: application/json``
and redirects a plain form submit to ``/dashboard``, so the page's own
script and a frontend of its own use the same routes.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Security
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app.core.auth import get_web_user
from app.core.config import setup_page_enabled
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import AppOwner, User, in_department, issue_api_key
from app.routers.dashboard.common import router, script_response, wants_html, wants_json
from app.services import client_patch, client_rules, dashboard_view, model_changes


# ── API keys ──

@router.post("/dashboard/refresh-key")
def refresh_own_key(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Reissue the caller's own API key. The old one stops working at once."""
    # Re-fetch from DB for write operation (user was expunged in get_web_user)
    db_user = session.get(User, user.id)
    issue_api_key(db_user)
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return JSONResponse({"ok": True, "api_key": db_user.api_key})


def owned_service_account(session: Session, user: User, app_id: int) -> User:
    """The service account ``app_id`` when ``user`` may see and change it:
    assigned as an owner, and in the department it was made for
    (``schema.in_department``). 404 for an account that does not exist, 403
    otherwise. Every route that shows or changes a service account for its
    owner goes through this."""
    target = session.exec(select(User).where(User.id == app_id)).first()
    if not target:
        raise HTTPException(status_code=404, detail="Service account not found.")

    ownership = session.exec(
        select(AppOwner).where(AppOwner.app_id == app_id, AppOwner.owner_id == user.id)
    ).first()
    if not ownership:
        raise HTTPException(status_code=403, detail="You do not own this service account.")
    # Assigned, but made for a department the owner is no longer in: they
    # see nothing of it (``schema.in_department``), so they change nothing.
    owner = session.get(User, user.id)
    if owner is None or not in_department(owner, target):
        raise HTTPException(
            status_code=403,
            detail="This service account is for another department than yours. Ask an administrator.",
        )
    return target


@router.post("/dashboard/app/{app_id}/refresh-key")
def refresh_owned_app_key(
    app_id: int,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Reissue the API key of a service account owned by the current user."""
    target = owned_service_account(session, user, app_id)
    issue_api_key(target)
    session.add(target)
    session.commit()
    session.refresh(target)
    return JSONResponse({"ok": True, "api_key": target.api_key})


# ── Model changes ──

@router.get("/dashboard/model-changes/patch.{fmt}")
def model_changes_patch(
    fmt: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The account's model-change script: the renames of every change it is
    shown, for its tier (``model_changes.for_account``), as ``patch.bat``
    (Windows) or ``patch.sh`` (Linux / macOS). One script however many
    changes there are, and nothing but renames: the notices' patch is a
    different download. Mail links here too, so a browser arriving after
    the change was switched off is sent to the dashboard instead of a 404."""
    if fmt not in client_patch.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    changes = model_changes.for_account(session, user)
    renames: dict[str, str] = {}
    for change in changes:
        renames.update(change["renames"])
    if not renames:
        if wants_html(request):
            return RedirectResponse(url="/dashboard", status_code=303)
        raise HTTPException(status_code=404, detail="No model change for this account.")
    filename = f"{model_changes.PATCH_FILE}.{fmt}"
    name = "model change " + ", ".join(change["ref"] for change in changes)
    return script_response(client_patch.render_patch(fmt, name, {}, filename, renames), filename)


@router.post("/dashboard/model-changes/{change_id}/applied")
def model_change_applied(
    change_id: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The user says they switched to the new model: the change folds on
    their dashboard and they are no longer mailed about it. It stays listed,
    with its script, for as long as the admin shows it. The change is named
    by its number (``3``) or as the account is shown it (``MC3``, the ``id``
    in ``GET /dashboard/api/notices``)."""
    number = model_changes.parse_ref(change_id)
    if number is None or not model_changes.set_applied(session, number, user):
        raise HTTPException(status_code=404, detail="No such model change for this account.")
    logger.info("Model change marked as done by the account | change={} user={}", model_changes.ref(number), user.username)
    if wants_json(request):
        return JSONResponse({"ok": True, "id": number})
    return RedirectResponse(url="/dashboard", status_code=303)


# ── Client-rule notices ──

# "all" routes first: FastAPI matches in order, and no check id is "all".
@router.get("/dashboard/client-rules/all/patch.{fmt}")
def client_rules_patch_all(
    fmt: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The account's one patch: the patches of everything it has open
    merged (``client_rules.combined_for``; the variant for this account:
    local-only, Azure or AWS), as ``patch.bat`` (Windows) or ``patch.sh``
    (Linux / macOS). There is no per-check download: one notice or five, it
    is the same single script. Mail links here too, so a browser arriving
    with nothing left to apply is sent to the dashboard instead of a 404."""
    if fmt not in client_patch.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    combined = client_rules.combined_for(session, user)
    if combined is None or not combined["env"]:
        if wants_html(request):
            return RedirectResponse(url="/dashboard", status_code=303)
        raise HTTPException(status_code=404, detail="No settings patch for this account.")
    filename = f"claude-code-patch.{fmt}"
    name = "fixes for " + ", ".join(combined["covered"])
    return script_response(client_patch.render_patch(fmt, name, combined["env"], filename), filename)


@router.post("/dashboard/client-rules/all/resolved")
def client_rules_resolved_all(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """"I've applied everything": every open notice of the account goes
    away; each returns only if the gateway still sees its problem after
    ``client_rules.RESOLVED_QUIET_DAYS``."""
    count = client_rules.resolve_all(session, user.username)
    logger.info("Client rules all marked as fixed by the account | user={} count={}", user.username, count)
    if wants_json(request):
        return JSONResponse({"ok": True, "resolved": count})
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/client-rules/{rule_id}/resolved")
def client_rule_resolved(
    rule_id: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """The user says they applied the fix: their notice for this check goes
    away and they are no longer mailed or reminded about it. The gateway
    cannot see a settings file, so it takes their word for
    ``client_rules.RESOLVED_QUIET_DAYS``; a violating request after that
    reopens the notice."""
    if not client_rules.resolve(session, rule_id, user.username):
        raise HTTPException(status_code=404, detail="Nothing to mark as done for this account.")
    logger.info("Client rule marked as fixed by the account | rule={} user={}", rule_id, user.username)
    if wants_json(request):
        return JSONResponse({"ok": True, "rule_id": rule_id})
    return RedirectResponse(url="/dashboard", status_code=303)


# ── Scripts that hold the key ──

@router.get("/dashboard/api-key/patch.{fmt}")
def api_key_patch(
    fmt: str,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """A script that puts the account's current API key into Claude Code
    (``client_patch.key_env``: ``ANTHROPIC_AUTH_TOKEN`` set in the ``env``
    block of settings.json, the older ``ANTHROPIC_API_KEY`` removed, nothing
    else), as ``patch.bat`` or ``patch.sh``: what a user runs on each
    machine after regenerating the key. Only ever the caller's own key; the
    download is in the audit log (``audit._AUDITED_READS``) and never cached."""
    if fmt not in client_patch.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    filename = f"claude-code-key.{fmt}"
    script = client_patch.render_patch(
        fmt, f"your API key - {client_patch.API_KEY_ENV}", client_patch.key_env(user.api_key), filename,
    )
    logger.info("API key patch downloaded | user={} format={}", user.username, fmt)
    return script_response(script, filename, secret=True)


@router.get("/dashboard/install-claude-code.bat")
def personalized_claude_installer(
    user: User = Security(get_web_user, scopes=["read"]),
):
    """Serve install-claude-code.bat with the user's API key inlined.

    Auth'd so the key is never exposed publicly; oauth2-proxy redirects
    unauthenticated users through SSO first. 404 with the Setup page off
    (``SETUP_PAGE_ENABLED=false``).
    """
    if not setup_page_enabled():
        raise HTTPException(status_code=404, detail="Not found.")
    template_path = dashboard_view.SETUP_DIR / "install-claude-code.bat"
    if not template_path.is_file():
        raise HTTPException(status_code=404, detail="Installer not available.")
    script = template_path.read_text(encoding="utf-8").replace(
        "__USER_API_KEY__", user.api_key
    )
    # cmd.exe is fragile with LF-only files (it can merge lines, swallow REM
    # blocks, and end up running comment text as commands). Normalize all
    # line endings to CRLF regardless of how the operator saved the template
    # on the (typically Linux) gateway host.
    script = script.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return Response(
        content=script.encode("utf-8"),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="install-claude-code.bat"',
        },
    )
