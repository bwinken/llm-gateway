"""Client rules: who trips which client check, and how they are told.

The gateway cannot change a user's Claude Code settings. What it can do is
notice which accounts send requests of a certain kind and reach those people:

- **Checks** are code (``client_checks.CHECKS``), each with a violation id
  (``CC001``, ...). An admin does not write conditions; per check they set
  on / off, the action, the wording, the settings patch and mail
  (``client_rule_settings``; no row = the defaults in code). Nothing a
  check decides with is in the database.
- **Violation** (``client_violations``): an account tripped an enabled
  check. For ``WINDOW_DAYS`` after its last one the account sees the notice
  on its dashboard, with the patch (environment variables for the ``env``
  block of ``~/.claude/settings.json``, nothing else; a different set for
  local-only, Azure and AWS accounts) as text and as a Windows ``.bat``
  download.
- **Reminder** (``action = "remind"``): the first violating request of each
  local day is answered with the message as an error, so it shows in the
  terminal; the retry goes through. App accounts are never reminded.
- **Mail**: to ``<username>@<COMPANY_EMAIL_DOMAIN>`` (``mailer``), sent by
  an admin's click or by the ``client_rule_mail`` job for checks with
  ``auto_email``.
- **Versions** (``client_versions``): the Claude Code version in each
  Messages request's User-Agent, per account.

Everything on the request path reads a per-worker snapshot of the enabled
checks, writes at most hourly per account, and never fails a request: a
broken store or a check that raises means no notice, not an error. The one
deliberate exception is the reminder, which is an admin's explicit choice
per check.
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
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update
from starlette.concurrency import run_in_threadpool

from app.core.config import APP_TITLE
from app.core.database import engine
from app.core.logger import logger
from app.core.timeutil import local_today
from app.models.schema import ClientRuleSetting, ClientVersion, ClientViolation, User, is_app_account
from app.services import client_checks, mailer
from app.services.client_checks import Check, Facts, version_key
from app.services.scheduler import Job, env_seconds

# How long after an account's last violation its notice (and its place in
# the admin lists and mail runs) stays. A fixed client stops tripping the
# check, so a notice clears by itself.
WINDOW_DAYS = 7
# One write per account per worker per hour, per check and per version.
RESOLUTION_S = 3600
# Other workers pick up a settings change within this long.
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

# The settings patch only ever touches the ``env`` block of settings.json.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_MAX_VARS = 50
_ENV_MAX_VALUE = 2000
# Who gets which variant of a check's patch: every account gets the base
# (``patch``); an account with Azure access also gets ``patch_azure`` on top,
# one with AWS access ``patch_aws`` on top of that (AWS wins where both set
# the same variable). A local-only account gets the base alone.
AUDIENCES = ("local", "azure", "aws")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_version(user_agent: str | None) -> str:
    """Claude Code's version from a User-Agent (``claude-cli/2.1.302 (...)``),
    or "" for any other client."""
    match = _UA_VERSION.search(user_agent or "")
    return match.group(1) if match else ""


# ── Settings patch (environment variables only) ──

def parse_env(text: str | None) -> dict[str, str | None]:
    """An admin's patch text as ``{NAME: value}``; ``None`` means "remove
    this variable". One ``NAME=value`` per line, ``-NAME`` to remove, ``#``
    comments; or a JSON object of the same (``null`` to remove). Raises
    ValueError. Only environment variables can be expressed: the patch
    never reaches any other setting."""
    text = (text or "").strip()
    env: dict[str, str | None] = {}
    if not text:
        return env
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"Settings patch is not valid JSON: {exc}.")
        if not isinstance(data, dict):
            raise ValueError("Settings patch must be NAME=value lines or a JSON object of them.")
        for name, value in data.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (int, float)):
                value = str(value)
            elif value is not None and not isinstance(value, str):
                raise ValueError(f"{name}: an environment variable is text, not a list or an object.")
            env[str(name)] = value
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):
                env[line[1:].strip()] = None
                continue
            name, sep, value = line.partition("=")
            if not sep:
                raise ValueError(f"Settings patch: write NAME=value (or -NAME to remove), not {line[:40]!r}.")
            env[name.strip()] = value.strip()
    if len(env) > _ENV_MAX_VARS:
        raise ValueError(f"Settings patch has too many variables ({_ENV_MAX_VARS} at most).")
    for name, value in env.items():
        if not _ENV_NAME.match(name):
            raise ValueError(f"Settings patch: {name[:40]!r} is not an environment variable name.")
        if value is not None and (len(value) > _ENV_MAX_VALUE or "\n" in value or "\r" in value):
            raise ValueError(f"Settings patch: the value of {name} is too long or spans lines.")
    return env


def dump_env(env: dict[str, str | None]) -> str:
    """How a patch is stored: JSON, "" when empty."""
    return json.dumps(env, ensure_ascii=False) if env else ""


def load_env(stored: str | None) -> dict[str, str | None]:
    """A stored patch back as a dict ({} for anything unreadable)."""
    try:
        data = json.loads(stored) if stored else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): (None if v is None else str(v)) for k, v in data.items() if _ENV_NAME.match(str(k))}


def env_lines(env: dict[str, str | None]) -> str:
    """A patch as the admin edits it: ``NAME=value`` / ``-NAME`` lines."""
    return "\n".join(f"-{name}" if value is None else f"{name}={value}" for name, value in env.items())


def env_snippet(env: dict[str, str | None]) -> str:
    """A patch as the user reads it: the ``env`` block to have in
    settings.json, then the variables to take out of it."""
    sets = {name: value for name, value in env.items() if value is not None}
    removes = [name for name, value in env.items() if value is None]
    parts = []
    if sets:
        parts.append('"env": ' + json.dumps(sets, indent=2, ensure_ascii=False))
    if removes:
        parts.append("Remove from \"env\": " + ", ".join(removes))
    return "\n".join(parts)


def audience_of(user: User) -> tuple[bool, bool]:
    """``(azure, aws)``: which cloud backends the account may use. Admins
    bypass the flags, as on the dashboard."""
    return bool(user.can_use_azure or user.is_admin), bool(user.can_use_bedrock or user.is_admin)


def effective_env(rule: Any, azure: bool, aws: bool) -> dict[str, str | None]:
    """The patch one account gets: the base, then the Azure variant if it
    has Azure access, then the AWS variant if it has AWS access. ``rule``
    is anything with ``patch`` / ``patch_azure`` / ``patch_aws`` in stored form."""
    env = dict(load_env(rule.patch))
    if azure:
        env.update(load_env(rule.patch_azure))
    if aws:
        env.update(load_env(rule.patch_aws))
    return env


def apply_env(settings: dict, env: dict[str, str | None]) -> dict:
    """What the downloaded script does to settings.json, in place: set or
    remove variables in its ``env`` block and leave everything else alone."""
    block = settings.get("env")
    if block is None:
        block = settings["env"] = {}
    if not isinstance(block, dict):
        raise ValueError('"env" in settings.json is not an object')
    for name, value in env.items():
        if value is None:
            block.pop(name, None)
        else:
            block[name] = value
    return settings


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
REM  Sets a few environment variables in the "env" block of
REM  %USERPROFILE%\.claude\settings.json. Nothing else in the file is
REM  changed, and the previous file is saved next to it as
REM  settings.json.bak. The variables are listed on your gateway dashboard.
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
  if (settings.env === undefined || settings.env === null) settings.env = {};
  if (!isObj(settings.env)) throw new Error('"env" in settings.json is not an object');
  for (const k of Object.keys(patch)) {
    if (patch[k] === null) delete settings.env[k];
    else settings.env[k] = String(patch[k]);
  }
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
    if ((-not $settings.Contains('env')) -or ($null -eq $settings['env'])) { $settings['env'] = [ordered]@{} }
    if (-not ($settings['env'] -is [System.Collections.IDictionary])) { throw '"env" in settings.json is not an object' }
    foreach ($k in @($patch.Keys)) {
        $v = $patch[$k]
        if ($null -eq $v) {
            if ($settings['env'].Contains($k)) { $settings['env'].Remove($k) }
        } else {
            $settings['env'][$k] = [string]$v
        }
    }
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


def render_patch_bat(name: str, env: dict[str, str | None]) -> str:
    """The Windows script that applies ``env`` to the ``env`` block of the
    user's settings.json (``apply_env``)."""
    payload = base64.b64encode(json.dumps(env, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = (
        _PATCH_BAT.replace("__PATCH_B64__", payload)
        .replace("__TITLE__", _bat_text(APP_TITLE))
        .replace("__NAME__", _bat_text(name))
    )
    # cmd.exe misreads LF-only files; see web_ui.personalized_claude_installer.
    return script.replace("\r\n", "\n").replace("\n", "\r\n")


# ── The checks as configured ──

@dataclass(frozen=True)
class Rule:
    """A check with its settings: the admin's row when there is one, the
    check's defaults otherwise."""
    check: Check
    enabled: bool
    action: str
    title: str
    message: str
    patch: str          # stored form (JSON), like the columns
    patch_azure: str
    patch_aws: str
    email_subject: str
    auto_email: bool
    email_repeat_days: int
    customized: bool    # an admin has saved this check's form
    updated_by: str

    @property
    def id(self) -> str:
        return self.check.id

    @property
    def name(self) -> str:
        return self.check.name


def _default_patch(lines: str) -> str:
    return dump_env(parse_env(lines))


def _rule(check: Check, row: ClientRuleSetting | None) -> Rule:
    if row is None:
        return Rule(
            check=check, enabled=check.enabled, action="notice",
            title=check.title, message=check.message, patch=_default_patch(check.env),
            patch_azure=_default_patch(check.env_azure), patch_aws=_default_patch(check.env_aws),
            email_subject="", auto_email=False, email_repeat_days=0, customized=False, updated_by="",
        )
    return Rule(
        check=check, enabled=bool(row.enabled), action=row.action if row.action in ACTIONS else "notice",
        title=row.title or check.title, message=row.message or "",
        patch=row.patch or "", patch_azure=row.patch_azure or "", patch_aws=row.patch_aws or "",
        email_subject=row.email_subject or "", auto_email=bool(row.auto_email),
        email_repeat_days=int(row.email_repeat_days or 0), customized=True, updated_by=row.updated_by or "",
    )


def rules(session: Session) -> list[Rule]:
    """Every check that is not retired, in the order of the code, as configured."""
    rows = {row.rule_id: row for row in session.exec(select(ClientRuleSetting)).all()}
    return [_rule(check, rows.get(check.id)) for check in client_checks.ACTIVE]


def get_rule(session: Session, rule_id: str) -> Rule | None:
    check = client_checks.BY_ID.get(rule_id)
    if check is None or check.retired:
        return None
    return _rule(check, session.get(ClientRuleSetting, rule_id))


# ── Snapshot of the enabled checks (per worker) ──

_snapshot: tuple[Rule, ...] = tuple(_rule(check, None) for check in client_checks.ACTIVE if check.enabled)
_lock = threading.Lock()
_last: dict[tuple, float] = {}
_reminded: dict[tuple[str, str], str] = {}


def refresh(bind=None) -> bool:
    """Reload this worker's snapshot. Blocking; never raises. On failure the
    previous snapshot stays in force."""
    global _snapshot
    try:
        with Session(bind or engine) as session:
            _snapshot = tuple(rule for rule in rules(session) if rule.enabled)
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


def _touch_violation(session: Session, rule_id: str, username: str, version: str, now: datetime) -> None:
    where = (ClientViolation.rule_id == rule_id, ClientViolation.username == username)
    values: dict[str, Any] = {"last_seen_at": now}
    if version:
        values["client_version"] = version
    if session.execute(update(ClientViolation).where(*where).values(**values)).rowcount:
        session.commit()
        return
    session.add(ClientViolation(
        rule_id=rule_id, username=username, client_version=version, first_seen_at=now, last_seen_at=now,
    ))
    try:
        session.commit()
    except IntegrityError:  # another worker added it first
        session.rollback()


def _record(username: str, version: str, record_version: bool, rule_ids: list[str]) -> None:
    now = utcnow()
    with Session(engine) as session:
        if record_version and version:
            _touch_version(session, username, version, now)
        for rule_id in rule_ids:
            _touch_violation(session, rule_id, username, version, now)


def _claim_reminder(rule_id: str, username: str, version: str, today: str) -> bool:
    """True for exactly one request per check, account and local day, across
    workers: the conditional UPDATE is the claim."""
    with Session(engine) as session:
        _touch_violation(session, rule_id, username, version, utcnow())
        claimed = session.execute(
            update(ClientViolation)
            .where(ClientViolation.rule_id == rule_id, ClientViolation.username == username,
                   ClientViolation.reminded_on != today)
            .values(reminded_on=today)
        ).rowcount == 1
        session.commit()
        return claimed


def _reminder_response(rule: Rule) -> JSONResponse:
    public_url = mailer.settings()["public_url"]
    where = f" Details and the fix: {public_url}/dashboard ." if public_url else " Details and the fix are on your gateway dashboard."
    text = (
        f"{APP_TITLE} notice [{rule.id}]: {rule.title}. {rule.message}".rstrip()
        + where
        + " This notice appears once a day: send your message again to continue."
    )
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request_error", "message": text}},
        headers={"X-Gateway-Reminder": rule.id},
    )


def _violated(rule: Rule, facts: dict[str, Any]) -> bool:
    try:
        return bool(rule.check.detect(Facts(**facts)))
    except Exception as exc:  # a broken check finds nothing; it never breaks a request
        if _due(("err", rule.id)):
            logger.warning("Client check {} failed | error={}: {}", rule.id, type(exc).__name__, exc)
        return False


async def _observe(request: Any, user: User) -> JSONResponse | None:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    username = user.username
    version = parse_version(request.headers.get("user-agent"))
    model = body.get("model")
    azure, aws = audience_of(user)
    facts = {
        "body": body, "model": model if isinstance(model, str) else "", "version": version,
        # The scheme the client used: uvicorn's proxy-headers handling sets
        # it from nginx's X-Forwarded-Proto (trusted peers only), like the
        # client address. Never read the header here: the client can send
        # its own.
        "scheme": request.url.scheme, "azure_access": azure, "aws_access": aws,
    }
    violated = [rule for rule in _snapshot if _violated(rule, facts)]

    record_version = bool(version) and _due(("v", username, version))
    rule_ids = [rule.id for rule in violated if _due(("h", rule.id, username))]
    if record_version or rule_ids:
        await run_in_threadpool(_record, username, version, record_version, rule_ids)

    if is_app_account(username):
        return None
    today = local_today().isoformat()
    for rule in violated:
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
    and violations; returns a response only for a due reminder. Never raises."""
    try:
        return await _observe(request, user)
    except Exception as exc:
        logger.warning(
            "Client rules check failed | user={} error={}: {}",
            getattr(user, "username", "-"), type(exc).__name__, exc,
        )
        return None


# ── Admin: settings of a check ──

def clean_fields(form: dict[str, Any]) -> dict[str, Any]:
    """Validate an admin's form for a check. Returns column values; raises ValueError."""
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
        "enabled": bool(form.get("enabled")), "action": action, "title": title, "message": message,
        "patch": dump_env(parse_env(str(form.get("patch") or ""))),
        "patch_azure": dump_env(parse_env(str(form.get("patch_azure") or ""))),
        "patch_aws": dump_env(parse_env(str(form.get("patch_aws") or ""))),
        "email_subject": str(form.get("email_subject") or "").strip()[:200],
        "auto_email": bool(form.get("auto_email")), "email_repeat_days": repeat,
    }


def save_settings(session: Session, check: Check, values: dict[str, Any], updated_by: str = "") -> Rule:
    row = session.get(ClientRuleSetting, check.id)
    if row is None:
        row = ClientRuleSetting(rule_id=check.id, updated_at=utcnow())
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by = updated_by
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _rule(check, row)


def reset_settings(session: Session, rule_id: str) -> None:
    """Back to the check's defaults in code. Its violations are kept."""
    row = session.get(ClientRuleSetting, rule_id)
    if row is not None:
        session.delete(row)
        session.commit()


# ── Violations ──

def _active_since(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=WINDOW_DAYS)


def violations(session: Session, rule_id: str, now: datetime | None = None) -> list[ClientViolation]:
    """A check's accounts inside the window, newest first."""
    return list(session.exec(
        select(ClientViolation)
        .where(ClientViolation.rule_id == rule_id, ClientViolation.last_seen_at >= _active_since(now))
        .order_by(ClientViolation.last_seen_at.desc())  # type: ignore[attr-defined]
    ).all())


def notices_for(session: Session, user: User, now: datetime | None = None) -> list[dict[str, Any]]:
    """What ``user``'s dashboard shows: the enabled checks it tripped inside
    the window, each with the patch meant for this account."""
    rows = {
        row.rule_id: row for row in session.exec(
            select(ClientViolation)
            .where(ClientViolation.username == user.username, ClientViolation.last_seen_at >= _active_since(now))
        ).all()
    }
    if not rows:
        return []
    azure, aws = audience_of(user)
    return [
        {"rule_id": rule.id, "title": rule.title, "message": rule.message,
         "patch": env_snippet(effective_env(rule, azure, aws)),
         "seen_at": rows[rule.id].last_seen_at, "client_version": rows[rule.id].client_version}
        for rule in rules(session) if rule.enabled and rule.id in rows
    ]


def patch_for(session: Session, rule_id: str, user: User) -> tuple[Rule, dict[str, str | None]] | None:
    """The check and the patch meant for ``user``, if it tripped the check
    and there is something to apply. None otherwise."""
    rule = get_rule(session, rule_id)
    if rule is None:
        return None
    hit = session.exec(
        select(ClientViolation).where(ClientViolation.rule_id == rule_id, ClientViolation.username == user.username)
    ).first()
    if hit is None:
        return None
    env = effective_env(rule, *audience_of(user))
    return (rule, env) if env else None


def audience_flags(audience: str) -> tuple[bool, bool]:
    """``(azure, aws)`` for an admin's preview of one audience: ``local``,
    ``azure`` (Azure only) or ``aws`` (AWS only)."""
    if audience not in AUDIENCES:
        raise ValueError(f"Audience must be one of: {', '.join(AUDIENCES)}.")
    return audience == "azure", audience == "aws"


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

def render_mail(rule: Any, username: str, env: dict[str, str | None]) -> tuple[str, str]:
    """``(subject, html)`` of a check's mail to one account. ``rule`` needs
    ``id``, ``title``, ``message`` and ``email_subject`` (a Rule, or a form
    being tested); ``env`` is the patch meant for that account. Everything
    an admin wrote is escaped."""
    public_url = mailer.settings()["public_url"]
    subject = (rule.email_subject or f"[{APP_TITLE}] {rule.title}").strip()
    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;line-height:1.6">',
        f"<p>Hi {html.escape(username)},</p>",
        f'<p style="font-size:16px;font-weight:600;margin-bottom:4px">{html.escape(rule.title)}</p>',
    ]
    if rule.message:
        parts.append("<p>" + html.escape(rule.message).replace("\n", "<br>") + "</p>")
    if env:
        parts.append(
            "<p>Environment settings for <code>~/.claude/settings.json</code>:</p>"
            '<pre style="background:#f4f4f4;padding:10px;border-radius:4px;font-size:13px">'
            + html.escape(env_snippet(env)) + "</pre>"
        )
    if public_url:
        link = html.escape(f"{public_url}/dashboard", quote=True)
        what = "see this notice and download a script that applies the settings for you (Windows)" if env else "see this notice"
        parts.append(f'<p><a href="{link}">Open your dashboard</a> to {what}.</p>')
    parts.append(
        f'<p style="color:#888;font-size:12px">Sent by {html.escape(APP_TITLE)} because of requests from your account. '
        f"Reference: {html.escape(rule.id)}.</p></div>"
    )
    return subject, "".join(parts)


def mailable(session: Session, rows: list[ClientViolation]) -> list[ClientViolation]:
    """Violations whose account still exists and has a mailbox (not an app account)."""
    names = [v.username for v in rows if not is_app_account(v.username)]
    if not names:
        return []
    known = set(session.exec(select(User.username).where(User.username.in_(names))).all())  # type: ignore[attr-defined]
    return [v for v in rows if v.username in known]


def mail_targets(session: Session, rule_id: str, only_unmailed: bool, now: datetime | None = None) -> list[ClientViolation]:
    """Who a manual send reaches: the check's accounts inside the window."""
    rows = mailable(session, violations(session, rule_id, now))
    return [v for v in rows if v.mailed_at is None] if only_unmailed else rows


def _send_to(session: Session, rule: Rule, targets: list[ClientViolation]) -> tuple[int, int]:
    """Mail each target and stamp the outcome on its violation. Returns (sent, failed)."""
    names = [v.username for v in targets]
    access = {
        u.username: audience_of(u)
        for u in (session.exec(select(User).where(User.username.in_(names))).all() if names else [])  # type: ignore[attr-defined]
    }
    jobs = [(v.id, v.username) for v in targets]
    session.commit()                # nothing is held open while mail is being sent
    sent = failed = 0
    for violation_id, username in jobs:
        azure, aws = access.get(username, (False, False))
        subject, body = render_mail(rule, username, effective_env(rule, azure, aws))
        ok, error = mailer.send(mailer.address_for(username), subject, body)
        now = utcnow()
        values: dict[str, Any] = {"mail_attempted_at": now, "mail_error": "" if ok else error}
        if ok:
            values["mailed_at"] = now
            sent += 1
        else:
            failed += 1
        session.execute(update(ClientViolation).where(ClientViolation.id == violation_id).values(**values))
        session.commit()
    return sent, failed


def send_rule_mail(rule_id: str, only_unmailed: bool = True, bind=None) -> dict[str, int]:
    """A manual send, run after the admin's click returned. Blocking; own session."""
    try:
        with Session(bind or engine) as session:
            rule = get_rule(session, rule_id)
            if rule is None or not mailer.is_enabled():
                return {"sent": 0, "failed": 0}
            sent, failed = _send_to(session, rule, mail_targets(session, rule_id, only_unmailed))
        logger.info("Client rule mail | rule={} sent={} failed={}", rule_id, sent, failed)
        return {"sent": sent, "failed": failed}
    except Exception as exc:
        logger.warning("Client rule mail failed | rule={} error={}: {}", rule_id, type(exc).__name__, exc)
        return {"sent": 0, "failed": 0}


def _auto_due(violation: ClientViolation, repeat_days: int, now: datetime) -> bool:
    if violation.mail_attempted_at is None:
        return True
    if violation.mail_attempted_at > now - timedelta(seconds=MAIL_RETRY_S):
        return False
    if violation.mail_error or violation.mailed_at is None:
        return True  # the last attempt failed
    return repeat_days > 0 and violation.mailed_at <= now - timedelta(days=repeat_days)


def run_auto_mail(session: Session, now: datetime | None = None) -> str:
    """The ``client_rule_mail`` job: mail the accounts of every enabled check
    with ``auto_email`` that have not been mailed (or are due again)."""
    if not mailer.is_enabled():
        return "mail not configured"
    now = now or utcnow()
    auto = [rule for rule in rules(session) if rule.enabled and rule.auto_email]
    sent = failed = 0
    budget = MAIL_BATCH
    for rule in auto:
        if budget <= 0:
            break
        due = [
            v for v in mailable(session, violations(session, rule.id, now))
            if _auto_due(v, rule.email_repeat_days, now)
        ][:budget]
        budget -= len(due)
        s, f = _send_to(session, rule, due)
        sent, failed = sent + s, failed + f
    return f"rules={len(auto)} sent={sent} failed={failed}"


MAIL_JOB = Job(
    name="client_rule_mail",
    interval=lambda: env_seconds("CLIENT_RULE_MAIL_INTERVAL_S", 600.0),
    run=run_auto_mail,
)
