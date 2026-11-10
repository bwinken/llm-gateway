"""Admin-written rules about client requests, and the Claude Code versions in use.

The gateway cannot change a user's Claude Code settings. What it can do is
notice which accounts send requests of a certain kind and reach those people:

- **Rule** (``client_rules``): conditions on a Messages request (a body
  field is present, Claude Code is older than a version, the model alias)
  for everyone or for listed accounts / org codes.
- **Hit** (``client_rule_hits``): an account matched a rule. For
  ``WINDOW_DAYS`` after its last match the account sees the rule's message
  on its dashboard, with the rule's ``patch`` (a JSON Merge Patch for
  ``~/.claude/settings.json``) as text and as a Windows ``.bat`` download.
- **Reminder** (``action = "remind"``): the first matching request of each
  local day is answered with the message as an error, so it shows in the
  terminal; the retry goes through. App accounts are never reminded.
- **Mail**: to ``<username>@<COMPANY_EMAIL_DOMAIN>`` (``mailer``), sent by
  an admin's click or by the ``client_rule_mail`` job for rules with
  ``auto_email``.
- **Versions** (``client_versions``): the Claude Code version in each
  Messages request's User-Agent, per account.

Everything on the request path reads a per-worker snapshot of the rules,
writes at most hourly per account, and never fails a request: a broken rule
store means no notice, not an error. The one deliberate exception is the
reminder, which is an admin's explicit choice per rule.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update
from starlette.concurrency import run_in_threadpool

from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import local_today
from app.models.schema import ClientRule, ClientRuleHit, ClientVersion, User, is_app_account
from app.services import ip_allowlist, mailer
from app.services.scheduler import Job, env_seconds

# How long after an account's last match its notice (and its place in the
# admin lists and mail runs) stays. A fixed client stops matching, so a
# notice clears by itself.
WINDOW_DAYS = 7
# One write per account per worker per hour, per rule and per version.
RESOLUTION_S = 3600
# Other workers pick up a rule change within this long.
REFRESH_S = 15.0
# The version overview counts accounts seen in this many days.
VERSION_WINDOW_DAYS = 30

ACTIONS = ("notice", "remind")
CLIENT = "claude-code"

# Automatic mail: a failed send is retried after this long, and one run
# sends at most this many.
MAIL_RETRY_S = 6 * 3600
MAIL_BATCH = 200

_UA_VERSION = re.compile(r"claude-cli/(\d+(?:\.\d+){1,3})", re.IGNORECASE)
_VERSION = re.compile(r"^\d+(?:\.\d+){0,3}$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

_PATCH_MAX_BYTES = 16 * 1024
_PATCH_MAX_DEPTH = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Versions ──

def parse_version(user_agent: str | None) -> str:
    """Claude Code's version from a User-Agent (``claude-cli/2.1.302 (...)``),
    or "" for any other client."""
    match = _UA_VERSION.search(user_agent or "")
    return match.group(1) if match else ""


def version_key(version: str) -> tuple[int, ...]:
    """``"2.1.302"`` → ``(2, 1, 302)``, for ordering."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


# ── Snapshot of the enabled rules (per worker) ──

@dataclass(frozen=True)
class _Rule:
    id: int
    body_field: str
    version_below: tuple[int, ...]
    models: frozenset[str]
    usernames: frozenset[str]
    org_codes: frozenset[str]  # casefolded
    action: str
    title: str
    message: str

    def matches(self, body: dict, version: str, username: str, org_code: str) -> bool:
        if not (self.body_field or self.version_below or self.models):
            return False
        if self.usernames or self.org_codes:
            if username not in self.usernames and not (org_code and org_code.casefold() in self.org_codes):
                return False
        if self.body_field and self.body_field not in body:
            return False
        if self.version_below:
            key = version_key(version) if version else ()
            if not key or key >= self.version_below:
                return False
        if self.models:
            model = body.get("model")
            if not isinstance(model, str) or model not in self.models:
                return False
        return True


_snapshot: tuple[_Rule, ...] = ()
_lock = threading.Lock()
_last: dict[tuple, float] = {}
_reminded: dict[tuple[int, str], str] = {}


def _load(session: Session) -> tuple[_Rule, ...]:
    rows = session.exec(select(ClientRule).where(ClientRule.enabled == True)).all()  # noqa: E712
    return tuple(
        _Rule(
            id=row.id, body_field=row.body_field or "", version_below=version_key(row.version_below or ""),
            models=frozenset(str(m) for m in (row.models or [])),
            usernames=frozenset(str(u) for u in (row.usernames or [])),
            org_codes=frozenset(str(c).casefold() for c in (row.org_codes or [])),
            action=row.action, title=row.title, message=row.message,
        )
        for row in rows
    )


def refresh(bind=None) -> bool:
    """Reload this worker's snapshot. Blocking; never raises. On failure the
    previous snapshot stays in force."""
    global _snapshot
    try:
        with Session(bind or engine) as session:
            _snapshot = _load(session)
        return True
    except Exception as exc:
        logger.warning("Client rules refresh failed | error={}: {}", type(exc).__name__, exc)
        return False


def reset() -> None:
    """Forget the snapshot and the write throttles (tests)."""
    global _snapshot
    _snapshot = ()
    with _lock:
        _last.clear()
        _reminded.clear()


async def refresh_loop(interval: float = REFRESH_S) -> None:
    """Background task, one per worker (started from the lifespan)."""
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(refresh)


def _due(key: tuple, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _lock:
        last = _last.get(key)
        if last is not None and now - last < RESOLUTION_S:
            return False
        if len(_last) > 50_000:  # a bound, not a cache policy
            _last.clear()
        _last[key] = now
        return True


# ── Request path ──

def _touch_version(session: Session, username: str, version: str, now: datetime) -> None:
    where = (ClientVersion.username == username, ClientVersion.client == CLIENT, ClientVersion.version == version)
    if session.execute(update(ClientVersion).where(*where).values(last_seen_at=now)).rowcount:
        session.commit()
        return
    session.add(ClientVersion(username=username, client=CLIENT, version=version, first_seen_at=now, last_seen_at=now))
    try:
        session.commit()
    except IntegrityError:  # another worker added it first
        session.rollback()


def _touch_hit(session: Session, rule_id: int, username: str, version: str, now: datetime) -> None:
    where = (ClientRuleHit.rule_id == rule_id, ClientRuleHit.username == username)
    values: dict[str, Any] = {"last_seen_at": now}
    if version:
        values["client_version"] = version
    if session.execute(update(ClientRuleHit).where(*where).values(**values)).rowcount:
        session.commit()
        return
    session.add(ClientRuleHit(
        rule_id=rule_id, username=username, client_version=version, first_seen_at=now, last_seen_at=now,
    ))
    try:
        session.commit()
    except IntegrityError:  # another worker added it first, or the rule was just deleted
        session.rollback()


def _record(username: str, version: str, record_version: bool, rule_ids: list[int]) -> None:
    now = utcnow()
    with Session(engine) as session:
        if record_version and version:
            _touch_version(session, username, version, now)
        for rule_id in rule_ids:
            _touch_hit(session, rule_id, username, version, now)


def _claim_reminder(rule_id: int, username: str, version: str, today: str) -> bool:
    """True for exactly one request per rule, account and local day, across
    workers: the conditional UPDATE is the claim."""
    with Session(engine) as session:
        _touch_hit(session, rule_id, username, version, utcnow())
        claimed = session.execute(
            update(ClientRuleHit)
            .where(ClientRuleHit.rule_id == rule_id, ClientRuleHit.username == username,
                   ClientRuleHit.reminded_on != today)
            .values(reminded_on=today)
        ).rowcount == 1
        session.commit()
        return claimed


def _reminder_response(rule: _Rule) -> JSONResponse:
    public_url = mailer.settings()["public_url"]
    where = f" Details and the fix: {public_url}/dashboard ." if public_url else " Details and the fix are on your gateway dashboard."
    text = (
        f"{APP_TITLE} notice: {rule.title}. {rule.message}".rstrip()
        + where
        + " This notice appears once a day: send your message again to continue."
    )
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request_error", "message": text}},
        headers={"X-Gateway-Reminder": str(rule.id)},
    )


async def _observe(request: Any, user: User) -> JSONResponse | None:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    username = user.username
    version = parse_version(request.headers.get("user-agent"))
    matched = [r for r in _snapshot if r.matches(body, version, username, user.org_code or "")]

    record_version = bool(version) and _due(("v", username, version))
    hit_ids = [r.id for r in matched if _due(("h", r.id, username))]
    if record_version or hit_ids:
        await run_in_threadpool(_record, username, version, record_version, hit_ids)

    if is_app_account(username):
        return None
    today = local_today().isoformat()
    for rule in matched:
        if rule.action != "remind":
            continue
        key = (rule.id, username)
        with _lock:
            if _reminded.get(key) == today:
                continue
            if len(_reminded) > 50_000:
                _reminded.clear()
            _reminded[key] = today
        if await run_in_threadpool(_claim_reminder, rule.id, username, version, today):
            logger.info("Client rule reminder | rule={} user={}", rule.id, username)
            return _reminder_response(rule)
    return None


async def observe_request(request: Any, user: User) -> JSONResponse | None:
    """Called at the top of every Messages route. Records the client version
    and rule matches; returns a response only for a due reminder. Never raises."""
    try:
        return await _observe(request, user)
    except Exception as exc:
        logger.warning(
            "Client rules check failed | user={} error={}: {}",
            getattr(user, "username", "-"), type(exc).__name__, exc,
        )
        return None


# ── Settings patch ──

def _check_patch_value(value: Any, depth: int) -> None:
    if depth > _PATCH_MAX_DEPTH:
        raise ValueError("Settings patch is nested too deeply.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not key or len(key) > 200 or key in ("__proto__", "constructor", "prototype"):
                raise ValueError(f"Settings patch has an unusable key: {key[:40]!r}.")
            _check_patch_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_patch_value(item, depth + 1)


def parse_patch(text: str | None) -> dict | None:
    """The settings patch as a dict (None when empty). Raises ValueError for
    anything that is not a non-empty JSON object of reasonable size."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > _PATCH_MAX_BYTES:
        raise ValueError("Settings patch is too large (16 KB at most).")
    try:
        patch = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"Settings patch is not valid JSON: {exc}.")
    if not isinstance(patch, dict) or not patch:
        raise ValueError('Settings patch must be a JSON object, e.g. {"env": {"NAME": "value"}}.')
    _check_patch_value(patch, 1)
    return patch


def format_patch(patch: dict | None) -> str:
    return json.dumps(patch, indent=2, ensure_ascii=False) if patch else ""


def merge_patch(target: dict, patch: dict) -> dict:
    """JSON Merge Patch (RFC 7396), in place: objects merge key by key,
    ``null`` removes a key, anything else replaces. The downloaded script
    does the same on the user's machine."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            merge_patch(target[key], value)
        else:
            target[key] = value
    return target


# One file that cmd.exe, Node and PowerShell each read their own part of.
# The patch travels as base64, so nothing an admin types is ever parsed by
# cmd.exe. Node is preferred (an exact JSON round trip); Windows PowerShell
# is the fallback on machines without it.
_PATCH_BAT = r"""@echo off
setlocal
REM ============================================================
REM  __TITLE__
REM  Settings patch for Claude Code: __NAME__
REM
REM  Merges a few settings into %USERPROFILE%\.claude\settings.json.
REM  Everything else in the file is kept, and the previous file is
REM  saved next to it as settings.json.bak. The settings are listed
REM  on your gateway dashboard.
REM ============================================================
set "GW_PATCH_SELF=%~f0"
echo.
echo  __TITLE__ - Claude Code settings patch
echo  __NAME__
echo.
where node >nul 2>&1
if errorlevel 1 goto use_powershell
node -e "const fs=require('fs');const t=fs.readFileSync(process.env.GW_PATCH_SELF,'utf8');const a=t.indexOf('//GW-NODE-'+'BEGIN'),b=t.indexOf('//GW-NODE-'+'END');try{eval(t.slice(a,b))}catch(e){console.error('   [ERR] '+e.message);process.exit(1)}"
goto done
:use_powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:GW_PATCH_SELF); $i=$t.IndexOf('#GW-PATCH-BEGIN'+[char]35); Invoke-Expression $t.Substring($i)"
:done
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" echo    Done. Restart Claude Code to apply the new settings.
if not "%RC%"=="0" echo    The patch was not applied. Your settings were not changed.
echo.
pause
exit /b %RC%

//GW-NODE-BEGIN
(function () {
  const fs = require('fs'), path = require('path'), os = require('os');
  const patch = JSON.parse(Buffer.from('__PATCH_B64__', 'base64').toString('utf8'));
  const dir = path.join(os.homedir(), '.claude');
  const file = path.join(dir, 'settings.json');
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  let settings = {};
  if (fs.existsSync(file)) {
    const text = fs.readFileSync(file, 'utf8').replace(/^﻿/, '');
    if (text.trim()) {
      settings = JSON.parse(text);
      if (!isObj(settings)) throw new Error('settings.json is not a JSON object');
    }
    fs.copyFileSync(file, file + '.bak');
  } else {
    fs.mkdirSync(dir, { recursive: true });
  }
  const merge = (t, p) => {
    for (const k of Object.keys(p)) {
      const v = p[k];
      if (v === null) delete t[k];
      else if (isObj(v)) { if (!isObj(t[k])) t[k] = {}; merge(t[k], v); }
      else t[k] = v;
    }
  };
  merge(settings, patch);
  fs.writeFileSync(file, JSON.stringify(settings, null, 2) + '\n', 'utf8');
  console.log('   [OK] Updated ' + file);
})();
//GW-NODE-END

#GW-PATCH-BEGIN#
$ErrorActionPreference = 'Stop'
try {
    function GwToHash($o) {
        if ($o -is [System.Management.Automation.PSCustomObject]) {
            $h = [ordered]@{}
            foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = GwToHash $p.Value }
            return $h
        }
        if ($o -is [System.Array]) {
            $a = New-Object System.Collections.ArrayList
            foreach ($i in $o) { [void]$a.Add((GwToHash $i)) }
            return ,$a.ToArray()
        }
        return $o
    }
    function GwMerge($t, $p) {
        foreach ($k in @($p.Keys)) {
            $v = $p[$k]
            if ($null -eq $v) {
                if ($t.Contains($k)) { $t.Remove($k) }
            } elseif ($v -is [System.Collections.IDictionary]) {
                if (-not ($t.Contains($k) -and ($t[$k] -is [System.Collections.IDictionary]))) { $t[$k] = [ordered]@{} }
                GwMerge $t[$k] $v
            } else {
                $t[$k] = $v
            }
        }
    }
    $patchText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATCH_B64__'))
    $patch = GwToHash ($patchText | ConvertFrom-Json)
    $dir = Join-Path $env:USERPROFILE '.claude'
    $file = Join-Path $dir 'settings.json'
    $settings = [ordered]@{}
    if (Test-Path -LiteralPath $file) {
        $text = [IO.File]::ReadAllText($file)
        if ($text.Trim()) {
            $parsed = $text | ConvertFrom-Json
            if (-not ($parsed -is [System.Management.Automation.PSCustomObject])) { throw 'settings.json is not a JSON object' }
            $settings = GwToHash $parsed
        }
        Copy-Item -LiteralPath $file -Destination ($file + '.bak') -Force
    } elseif (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    GwMerge $settings $patch
    $json = ConvertTo-Json -InputObject $settings -Depth 100
    [IO.File]::WriteAllText($file, $json + "`r`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Host ('   [OK] Updated ' + $file)
} catch {
    Write-Host ('   [ERR] ' + $_.Exception.Message)
    exit 1
}
"""


def _bat_text(text: str) -> str:
    """Text safe to put on a ``REM`` / ``echo`` line: plain ASCII words only."""
    cleaned = re.sub(r"[^A-Za-z0-9 _.,:+-]", " ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()[:80] or "settings"


def render_patch_bat(name: str, patch: dict) -> str:
    """The Windows script that applies ``patch`` to the user's settings.json."""
    payload = base64.b64encode(json.dumps(patch, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = (
        _PATCH_BAT.replace("__PATCH_B64__", payload)
        .replace("__TITLE__", _bat_text(APP_TITLE))
        .replace("__NAME__", _bat_text(name))
    )
    # cmd.exe misreads LF-only files; see web_ui.personalized_claude_installer.
    return script.replace("\r\n", "\n").replace("\n", "\r\n")


# ── Admin: rules ──

def clean_fields(session: Session, form: dict[str, Any]) -> dict[str, Any]:
    """Validate an admin's rule form. Returns column values; raises ValueError."""
    name = str(form.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValueError("Give the rule a name (100 characters at most).")
    body_field = str(form.get("body_field") or "").strip()
    if body_field and not _FIELD.match(body_field):
        raise ValueError("Request field must be one field name, e.g. safeguards.")
    version_below = str(form.get("version_below") or "").strip().lstrip("vV")
    if version_below and not _VERSION.match(version_below):
        raise ValueError("Version must look like 2.1.302.")
    models = ip_allowlist.split_names(str(form.get("models") or ""))
    if not (body_field or version_below or models):
        raise ValueError("Set at least one condition: a request field, a version or a model.")
    usernames = ip_allowlist.split_names(str(form.get("usernames") or ""))
    org_codes = ip_allowlist.split_names(str(form.get("org_codes") or ""))
    usernames, org_codes = ip_allowlist.validate_who(session, usernames, org_codes)
    title = str(form.get("title") or "").strip()
    if not title or len(title) > 200:
        raise ValueError("Give the notice a title (200 characters at most).")
    message = str(form.get("message") or "").strip()
    if len(message) > 4000:
        raise ValueError("The message is too long (4000 characters at most).")
    action = str(form.get("action") or "notice").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"Action must be one of: {', '.join(ACTIONS)}.")
    try:
        repeat = int(str(form.get("email_repeat_days") or "0").strip() or 0)
    except ValueError:
        raise ValueError("Repeat days must be a whole number.")
    if not 0 <= repeat <= 365:
        raise ValueError("Repeat days must be between 0 and 365.")
    return {
        "name": name, "body_field": body_field, "version_below": version_below, "models": models,
        "usernames": usernames, "org_codes": org_codes, "title": title, "message": message,
        "patch": format_patch(parse_patch(str(form.get("patch") or ""))), "action": action,
        "email_subject": str(form.get("email_subject") or "").strip()[:200],
        "auto_email": bool(form.get("auto_email")), "email_repeat_days": repeat,
        "enabled": bool(form.get("enabled")),
    }


def save_rule(session: Session, values: dict[str, Any], rule: ClientRule | None = None, created_by: str = "") -> ClientRule:
    now = utcnow()
    if rule is None:
        rule = ClientRule(created_by=created_by, created_at=now, updated_at=now, **values)
    else:
        for key, value in values.items():
            setattr(rule, key, value)
        rule.updated_at = now
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def delete_rule(session: Session, rule: ClientRule) -> None:
    for hit in session.exec(select(ClientRuleHit).where(ClientRuleHit.rule_id == rule.id)).all():
        session.delete(hit)
    session.flush()
    session.delete(rule)
    session.commit()


def condition_text(rule: ClientRule) -> str:
    parts = []
    if rule.body_field:
        parts.append(f"request has `{rule.body_field}`")
    if rule.version_below:
        parts.append(f"Claude Code < {rule.version_below}")
    if rule.models:
        parts.append("model " + " / ".join(rule.models))
    return " and ".join(parts) or "no condition"


def _active_since(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=WINDOW_DAYS)


def hits(session: Session, rule_id: int, now: datetime | None = None) -> list[ClientRuleHit]:
    """A rule's accounts inside the window, newest first."""
    return list(session.exec(
        select(ClientRuleHit)
        .where(ClientRuleHit.rule_id == rule_id, ClientRuleHit.last_seen_at >= _active_since(now))
        .order_by(ClientRuleHit.last_seen_at.desc())  # type: ignore[attr-defined]
    ).all())


def notices_for(session: Session, username: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """What ``username``'s dashboard shows: the enabled rules it matched
    inside the window."""
    rows = session.exec(
        select(ClientRule, ClientRuleHit)
        .where(ClientRuleHit.rule_id == ClientRule.id, ClientRuleHit.username == username,
               ClientRuleHit.last_seen_at >= _active_since(now), ClientRule.enabled == True)  # noqa: E712
        .order_by(ClientRule.id)
    ).all()
    return [
        {"rule_id": rule.id, "title": rule.title, "message": rule.message, "patch": rule.patch,
         "seen_at": hit.last_seen_at, "client_version": hit.client_version}
        for rule, hit in rows
    ]


def patch_for(session: Session, rule_id: int, username: str | None) -> tuple[ClientRule, dict] | None:
    """The rule and its patch, for an account that matched it (``username``
    None: no such check, for an admin's preview). None when there is nothing
    to download."""
    rule = session.get(ClientRule, rule_id)
    if rule is None or not rule.patch:
        return None
    if username is not None:
        hit = session.exec(
            select(ClientRuleHit).where(ClientRuleHit.rule_id == rule_id, ClientRuleHit.username == username)
        ).first()
        if hit is None:
            return None
    try:
        patch = parse_patch(rule.patch)
    except ValueError:
        return None
    return (rule, patch) if patch else None


def version_overview(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Each account's newest Claude Code version seen in the last
    ``VERSION_WINDOW_DAYS`` days, grouped by version (newest version first)."""
    now = now or utcnow()
    rows = session.exec(
        select(ClientVersion)
        .where(ClientVersion.client == CLIENT, ClientVersion.last_seen_at >= now - timedelta(days=VERSION_WINDOW_DAYS))
    ).all()
    latest: dict[str, ClientVersion] = {}
    for row in rows:
        best = latest.get(row.username)
        if best is None or (row.last_seen_at, version_key(row.version)) > (best.last_seen_at, version_key(best.version)):
            latest[row.username] = row
    groups: dict[str, list[str]] = {}
    for username, row in latest.items():
        groups.setdefault(row.version, []).append(username)
    versions = [
        {"version": version, "usernames": sorted(names), "count": len(names)}
        for version, names in sorted(groups.items(), key=lambda kv: version_key(kv[0]), reverse=True)
    ]
    return {"versions": versions, "accounts": len(latest), "days": VERSION_WINDOW_DAYS}


# ── Mail ──

def render_mail(rule: Any, username: str) -> tuple[str, str]:
    """``(subject, html)`` of a rule's mail. ``rule`` needs ``name``,
    ``title``, ``message``, ``patch`` and ``email_subject`` (a ClientRule or
    a form being tested). Everything an admin wrote is escaped."""
    public_url = mailer.settings()["public_url"]
    subject = (rule.email_subject or f"[{APP_TITLE}] {rule.title}").strip()
    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;line-height:1.6">',
        f"<p>Hi {html.escape(username)},</p>",
        f'<p style="font-size:16px;font-weight:600;margin-bottom:4px">{html.escape(rule.title)}</p>',
    ]
    if rule.message:
        parts.append("<p>" + html.escape(rule.message).replace("\n", "<br>") + "</p>")
    if rule.patch:
        parts.append(
            "<p>Settings to merge into <code>~/.claude/settings.json</code>:</p>"
            '<pre style="background:#f4f4f4;padding:10px;border-radius:4px;font-size:13px">'
            + html.escape(rule.patch) + "</pre>"
        )
    if public_url:
        link = html.escape(f"{public_url}/dashboard", quote=True)
        what = "see this notice and download a script that applies the settings for you (Windows)" if rule.patch else "see this notice"
        parts.append(f'<p><a href="{link}">Open your dashboard</a> to {what}.</p>')
    parts.append(
        f'<p style="color:#888;font-size:12px">Sent by {html.escape(APP_TITLE)} because requests from your account '
        f"matched the rule &ldquo;{html.escape(rule.name)}&rdquo;.</p></div>"
    )
    return subject, "".join(parts)


def mailable(session: Session, rows: list[ClientRuleHit]) -> list[ClientRuleHit]:
    """Hits whose account still exists and has a mailbox (not an app account)."""
    names = [h.username for h in rows if not is_app_account(h.username)]
    if not names:
        return []
    known = set(session.exec(select(User.username).where(User.username.in_(names))).all())  # type: ignore[attr-defined]
    return [h for h in rows if h.username in known]


def mail_targets(session: Session, rule_id: int, only_unmailed: bool, now: datetime | None = None) -> list[ClientRuleHit]:
    """Who a manual send reaches: the rule's accounts inside the window."""
    rows = mailable(session, hits(session, rule_id, now))
    return [h for h in rows if h.mailed_at is None] if only_unmailed else rows


def _send_to(session: Session, rule: ClientRule, targets: list[ClientRuleHit]) -> tuple[int, int]:
    """Mail each target and stamp the outcome on its hit. Returns (sent, failed)."""
    # A plain copy: the session is committed between sends.
    rule_view = SimpleNamespace(
        name=rule.name, title=rule.title, message=rule.message, patch=rule.patch, email_subject=rule.email_subject,
    )
    jobs = [(h.id, h.username) for h in targets]
    session.commit()                # nothing is held open while mail is being sent
    sent = failed = 0
    for hit_id, username in jobs:
        subject, body = render_mail(rule_view, username)
        ok, error = mailer.send(mailer.address_for(username), subject, body)
        now = utcnow()
        values: dict[str, Any] = {"mail_attempted_at": now, "mail_error": "" if ok else error}
        if ok:
            values["mailed_at"] = now
            sent += 1
        else:
            failed += 1
        session.execute(update(ClientRuleHit).where(ClientRuleHit.id == hit_id).values(**values))
        session.commit()
    return sent, failed


def send_rule_mail(rule_id: int, only_unmailed: bool = True, bind=None) -> dict[str, int]:
    """A manual send, run after the admin's click returned. Blocking; own session."""
    try:
        with Session(bind or engine) as session:
            rule = session.get(ClientRule, rule_id)
            if rule is None or not mailer.is_enabled():
                return {"sent": 0, "failed": 0}
            sent, failed = _send_to(session, rule, mail_targets(session, rule_id, only_unmailed))
        logger.info("Client rule mail | rule={} sent={} failed={}", rule_id, sent, failed)
        return {"sent": sent, "failed": failed}
    except Exception as exc:
        logger.warning("Client rule mail failed | rule={} error={}: {}", rule_id, type(exc).__name__, exc)
        return {"sent": 0, "failed": 0}


def _auto_due(hit: ClientRuleHit, repeat_days: int, now: datetime) -> bool:
    if hit.mail_attempted_at is None:
        return True
    if hit.mail_attempted_at > now - timedelta(seconds=MAIL_RETRY_S):
        return False
    if hit.mail_error or hit.mailed_at is None:
        return True  # the last attempt failed
    return repeat_days > 0 and hit.mailed_at <= now - timedelta(days=repeat_days)


def run_auto_mail(session: Session, now: datetime | None = None) -> str:
    """The ``client_rule_mail`` job: mail the accounts of every enabled rule
    with ``auto_email`` that have not been mailed (or are due again)."""
    if not mailer.is_enabled():
        return "mail not configured"
    now = now or utcnow()
    rules = session.exec(
        select(ClientRule).where(ClientRule.enabled == True, ClientRule.auto_email == True)  # noqa: E712
    ).all()
    sent = failed = 0
    budget = MAIL_BATCH
    for rule in rules:
        if budget <= 0:
            break
        due = [h for h in mailable(session, hits(session, rule.id, now)) if _auto_due(h, rule.email_repeat_days, now)]
        due = due[:budget]
        budget -= len(due)
        s, f = _send_to(session, rule, due)
        sent, failed = sent + s, failed + f
    return f"rules={len(rules)} sent={sent} failed={failed}"


MAIL_JOB = Job(
    name="client_rule_mail",
    interval=lambda: env_seconds("CLIENT_RULE_MAIL_INTERVAL_S", 600.0),
    run=run_auto_mail,
)
