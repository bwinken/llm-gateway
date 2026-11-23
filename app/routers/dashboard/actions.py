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

from app.core import api_docs
from app.core.auth import get_web_user
from app.core.config import setup_page_enabled
from app.core.database import get_session
from app.core.logger import logger
from app.models.schema import AppOwner, User, in_department, issue_api_key
from app.routers.dashboard.common import router, script_response, wants_html, wants_json
from app.services import client_patch, client_rules, dashboard_view, model_changes


# ── API keys ──

@router.post(
    "/dashboard/refresh-key", summary="Regenerate the caller's API key",
    responses=api_docs.answers(api_docs.KeyRegenerated),
)
def refresh_own_key(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Issue the signed-in person a new API key. The old one stops working at once.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `ok` | bool | `true` |
    | `api_key` | string | The new key, in full |

    **Limits and things to know**

    - Every client that holds the old key stops working until it is given the new one. For Claude Code,
      `GET /dashboard/api-key/patch.sh` (or `.bat`) is a script that puts it in.
    - The new key is of the current format, so `legacy_key` in `GET /v1/me` becomes `false`.
    - Only by this sign-in: no API key can regenerate a key, its own included.
    - Refused (403) when another site's page sent it. In the audit log.

    **Errors.** Only the group's (401, 403).
    """
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


@router.post(
    "/dashboard/app/{app_id}/refresh-key", summary="Regenerate an owned service account's API key",
    responses={
        **api_docs.answers(api_docs.KeyRegenerated),
        404: {"description": "No account with that id."},
    },
)
def refresh_owned_app_key(
    app_id: int,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """Issue a new API key for a service account the signed-in person owns. The old one stops working at once.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `app_id`: the account's `id`, as `GET /v1/service-accounts` gives it.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `ok` | bool | `true` |
    | `api_key` | string | The new key, in full |

    **Limits and things to know**

    - The program that uses the account stops working until it is given the new key.
    - Only for an owner an admin assigned, and only while they are in the department the account is for.
    - Refused (403) when another site's page sent it. In the audit log.

    **Errors**

    | Status | When |
    |---|---|
    | 403 | The caller does not own the account, or it is for another department than theirs |
    | 404 | No account with that id |
    """
    target = owned_service_account(session, user, app_id)
    issue_api_key(target)
    session.add(target)
    session.commit()
    session.refresh(target)
    return JSONResponse({"ok": True, "api_key": target.api_key})


# ── Model changes ──

@router.get(
    "/dashboard/model-changes/patch.{fmt}", summary="Download the model-change script",
    responses={**api_docs.SCRIPT_200, 404: {"description": "`fmt` is neither `bat` nor `sh`, or no model change is shown to this account."}},
)
def model_changes_patch(
    fmt: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """A script that replaces the models being retired with their successors in Claude Code's settings.json.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `fmt`: `bat` (Windows) or `sh` (Linux, macOS).

    **Response.** The script, as a download.

    **Limits and things to know**

    - It carries the renames of every model change shown to the account, for its cloud access: the
      `model_changes.patch.models` of `GET /v1/notices`.
    - It replaces a name only where the value is exactly the old one (the `model` setting, and the `env`
      variables with `MODEL` in their name), keeps a bracket suffix (`old[1m]`), adds nothing, and keeps
      a backup of the file.
    - Renames only: the fix for a client notice is another download, `/dashboard/client-rules/all/patch.*`.
    - Mail about a model change links here. A browser that arrives when no change is shown any more is
      sent to `/dashboard` instead of a 404.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | `fmt` is neither `bat` nor `sh`, or no model change is shown to this account |
    """
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


@router.post(
    "/dashboard/model-changes/{change_id}/applied", summary="Report a model change as switched",
    responses={
        **api_docs.answers(api_docs.ModelChangeApplied),
        404: {"description": "No such model change is shown to this account."},
    },
)
def model_change_applied(
    change_id: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """*I've switched*: the signed-in person says they moved to the new model.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `change_id`: the change as `GET /v1/notices` names it (`MC3`), or its number (`3`).

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `ok` | bool | `true` |
    | `id` | integer | The change's number |

    **Limits and things to know**

    - The change folds on the person's dashboard and they are no longer mailed about it. It stays
      listed, with its script, for as long as the admin shows it: the script is for their other
      machines too.
    - Only by this sign-in: by API key a model change can be read (`GET /v1/notices`), not reported.
    - With `Accept: application/json` the answer is the JSON above. A plain form submit is answered
      with a redirect (303) to `/dashboard`.
    - Refused (403) when another site's page sent it. In the audit log.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | No such model change is shown to this account |
    """
    number = model_changes.parse_ref(change_id)
    if number is None or not model_changes.set_applied(session, number, user):
        raise HTTPException(status_code=404, detail="No such model change for this account.")
    logger.info("Model change marked as done by the account | change={} user={}", model_changes.ref(number), user.username)
    if wants_json(request):
        return JSONResponse({"ok": True, "id": number})
    return RedirectResponse(url="/dashboard", status_code=303)


# ── Client-rule notices ──

# "all" routes first: FastAPI matches in order, and no check id is "all".
@router.get(
    "/dashboard/client-rules/all/patch.{fmt}", summary="Download the fix for the client notices",
    responses={**api_docs.SCRIPT_200, 404: {"description": "`fmt` is neither `bat` nor `sh`, or the account has nothing a setting can fix."}},
)
def client_rules_patch_all(
    fmt: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """A script that applies the fix for everything the account's client notices can fix with a setting.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `fmt`: `bat` (Windows) or `sh` (Linux, macOS).

    **Response.** The script, as a download.

    **Limits and things to know**

    - One script for all the account's open notices: the top-level `patch` of `GET /v1/notices`.
    - It only sets or removes variables in the `env` block of Claude Code's settings.json, and keeps a
      backup of the file. It never holds a key.
    - A notice no setting can fix (an outdated client, a model the account has no access to) is not in
      it: `manual` in `GET /v1/notices` lists those.
    - Mail about the notices links here. A browser that arrives with nothing left to apply is sent to
      `/dashboard` instead of a 404.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | `fmt` is neither `bat` nor `sh`, or the account has nothing a setting can fix |
    """
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


@router.post(
    "/dashboard/client-rules/all/resolved", summary="Report every notice as applied (the page's form)",
    responses=api_docs.answers(api_docs.AllNoticesResolved),
)
def client_rules_resolved_all(
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """*I've applied everything*: every open notice of the signed-in person is marked as fixed.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `ok` | bool | `true` |
    | `resolved` | integer | How many notices were resolved |

    **Limits and things to know**

    - The rendered page's own form, and it goes when the page does. Anything else calls
      `POST /v1/notices/resolved`, which does the same and takes this sign-in too.
    - A notice comes back only if its problem is still seen after the quiet days (14).
    - With `Accept: application/json` the answer is the JSON above. A plain form submit is answered
      with a redirect (303) to `/dashboard`.
    - Refused (403) when another site's page sent it. In the audit log.

    **Errors.** Only the group's (401, 403).
    """
    count = client_rules.resolve_all(session, user.username)
    logger.info("Client rules all marked as fixed by the account | user={} count={}", user.username, count)
    if wants_json(request):
        return JSONResponse({"ok": True, "resolved": count})
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post(
    "/dashboard/client-rules/{rule_id}/resolved", summary="Report one notice as applied (the page's form)",
    responses={
        **api_docs.answers(api_docs.NoticeResolved),
        404: {"description": "The account has no such notice open."},
    },
)
def client_rule_resolved(
    rule_id: str,
    request: Request,
    user: User = Security(get_web_user, scopes=["read"]),
    session: Session = Depends(get_session),
):
    """*I've applied it*: one open notice of the signed-in person is marked as fixed.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `rule_id`: the notice's `id`, such as `CC001`.

    **Response**

    | Key | Type | Meaning |
    |---|---|---|
    | `ok` | bool | `true` |
    | `rule_id` | string | The notice |

    **Limits and things to know**

    - The rendered page's own form, and it goes when the page does. Anything else calls
      `POST /v1/notices/resolved` with `ids`, which does the same and takes this sign-in too.
    - The gateway cannot see a settings file and takes the person's word for the quiet days (14): the
      notice comes back only if its problem is still seen after that.
    - With `Accept: application/json` the answer is the JSON above. A plain form submit is answered
      with a redirect (303) to `/dashboard`.
    - Refused (403) when another site's page sent it. In the audit log.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | The account has no such notice open |
    """
    if not client_rules.resolve(session, rule_id, user.username):
        raise HTTPException(status_code=404, detail="Nothing to mark as done for this account.")
    logger.info("Client rule marked as fixed by the account | rule={} user={}", rule_id, user.username)
    if wants_json(request):
        return JSONResponse({"ok": True, "rule_id": rule_id})
    return RedirectResponse(url="/dashboard", status_code=303)


# ── Scripts that hold the key ──

@router.get(
    "/dashboard/api-key/patch.{fmt}", summary="Download a script that puts the API key into Claude Code",
    responses={**api_docs.SCRIPT_200, 404: {"description": "`fmt` is neither `bat` nor `sh`."}},
)
def api_key_patch(
    fmt: str,
    user: User = Security(get_web_user, scopes=["read"]),
):
    """A script that puts the signed-in person's current API key into Claude Code: what to run on each machine after regenerating the key.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Path.** `fmt`: `bat` (Windows) or `sh` (Linux, macOS).

    **Response.** The script, as a download. It holds the key.

    **Limits and things to know**

    - It sets `ANTHROPIC_AUTH_TOKEN` in the `env` block of settings.json, removes the older
      `ANTHROPIC_API_KEY`, and changes nothing else. It keeps a backup of the file and never prints
      the key.
    - Only ever the caller's own key.
    - Never cached (`Cache-Control: no-store`), and every download is in the audit log.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | `fmt` is neither `bat` nor `sh` |
    """
    if fmt not in client_patch.PATCH_FORMATS:
        raise HTTPException(status_code=404, detail="No such patch format.")
    filename = f"claude-code-key.{fmt}"
    script = client_patch.render_patch(
        fmt, f"your API key - {client_patch.API_KEY_ENV}", client_patch.key_env(user.api_key), filename,
    )
    logger.info("API key patch downloaded | user={} format={}", user.username, fmt)
    return script_response(script, filename, secret=True)


@router.get(
    "/dashboard/install-claude-code.bat", summary="Download the Windows installer for Claude Code",
    responses={**api_docs.SCRIPT_200, 404: {"description": "The Setup page is switched off, or this site ships no installer."}},
)
def personalized_claude_installer(
    user: User = Security(get_web_user, scopes=["read"]),
):
    """The Windows installer for Claude Code, with the signed-in person's API key already in it.

    **Sign-in.** SSO only: the session cookie, which the proxy in front turns into the access token. An API key is not accepted.

    **Response.** `install-claude-code.bat`, as a download. It holds the key.

    **Limits and things to know**

    - Windows only (a `.bat`, with Windows line endings whatever the template had).
    - The key in it is the one the account has now: after regenerating, download it again or run the
      key script (`/dashboard/api-key/patch.bat`).
    - Off for a site that set `SETUP_PAGE_ENABLED=false`; `site.installer` in `GET /v1/me` says whether
      it can be downloaded.

    **Errors**

    | Status | When |
    |---|---|
    | 404 | The Setup page is switched off, or this site ships no installer |
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
