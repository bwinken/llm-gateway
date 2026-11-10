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
- **Resolved**: the account can say "I've applied it" on its notice. The
  notice goes away at once and it is no longer mailed; the next violating
  request reopens it (the gateway cannot see a settings file, only requests).
- **Reminder** (``action = "remind"``): the first violating request of each
  local day is answered with the message as an error, so it shows in the
  terminal; the retry goes through. App accounts are never reminded.
- **Mail**: to ``<username>@<COMPANY_EMAIL_DOMAIN>`` (``mailer``), sent by
  an admin's click or by the ``client_rule_mail`` job for checks with
  ``auto_email``. One mail per account, about everything it has open, with
  one patch that covers it all (``open_items``, ``combined_env``).
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
from sqlmodel import Session, func, select, update
from starlette.concurrency import run_in_threadpool

from app.core import config
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
# A check with no violation for this long is suggested for retirement.
STALE_DAYS = 30

ACTIONS = ("notice", "remind")
CLIENT = "claude-code"

# A failed send is retried after this long.
MAIL_RETRY_S = 6 * 3600


def mail_pace() -> dict[str, Any]:
    """How fast mail goes out, so a long list never reaches the mail server
    as one burst: at most ``batch`` mails per run of the mail job, ``pause_s``
    apart, one run every ``interval_s``. Whatever is left waits for the next
    run. An admin's click is paced the same way as automatic mail."""
    return {
        "batch": max(1, int(env_seconds("CLIENT_RULE_MAIL_BATCH", 20))),
        "pause_s": env_seconds("CLIENT_RULE_MAIL_PAUSE_S", 2.0),
        "interval_s": env_seconds("CLIENT_RULE_MAIL_INTERVAL_S", 600.0),
    }

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


def baseline_env() -> dict[str, str | None]:
    """``client_checks.BASELINE_ENV``: what every patch also sets."""
    return parse_env(client_checks.BASELINE_ENV)


def effective_env(rule: Any, azure: bool, aws: bool) -> dict[str, str | None]:
    """The patch one account gets: the base, then the Azure variant if it
    has Azure access, then the AWS variant if it has AWS access, and, when
    that leaves anything to apply, the settings every patch carries
    (``baseline_env``; the check's own value wins). ``rule`` is anything
    with ``patch`` / ``patch_azure`` / ``patch_aws`` in stored form."""
    env = dict(load_env(rule.patch))
    if azure:
        env.update(load_env(rule.patch_azure))
    if aws:
        env.update(load_env(rule.patch_aws))
    if env:
        for name, value in baseline_env().items():
            env.setdefault(name, value)
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


# What the downloaded scripts do, once per runtime. Each must do exactly
# ``apply_env``: read settings.json (a missing or empty file is {}), refuse
# one that is not a JSON object or whose "env" is not an object, save a
# .bak, set or remove the variables in "env" and write the file back.
_NODE_APPLY = r"""(function () {
  const fs = require('fs'), path = require('path'), os = require('os');
  const patch = JSON.parse(Buffer.from('__PATCH_B64__', 'base64').toString('utf8'));
  const dir = path.join(os.homedir(), '.claude');
  const file = path.join(dir, 'settings.json');
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  let settings = {};
  if (fs.existsSync(file)) {
    const text = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '');
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
})();"""

_PYTHON_APPLY = r"""import base64, json, os, shutil, sys
patch = json.loads(base64.b64decode('__PATCH_B64__').decode('utf-8'))
folder = os.path.join(os.path.expanduser('~'), '.claude')
path = os.path.join(folder, 'settings.json')
settings = {}
if os.path.exists(path):
    with open(path, encoding='utf-8-sig') as f:
        text = f.read()
    if text.strip():
        settings = json.loads(text)
        if not isinstance(settings, dict):
            sys.exit('   [ERR] settings.json is not a JSON object')
    shutil.copyfile(path, path + '.bak')
else:
    os.makedirs(folder, exist_ok=True)
if settings.get('env') is None:
    settings['env'] = {}
if not isinstance(settings['env'], dict):
    sys.exit('   [ERR] "env" in settings.json is not an object')
for name, value in patch.items():
    if value is None:
        settings['env'].pop(name, None)
    else:
        settings['env'][name] = str(value)
with open(path, 'w', encoding='utf-8') as f:
    f.write(json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
print('   [OK] Updated ' + path)"""

# The Linux / macOS script: plain sh, with Node or Python 3 doing the JSON
# (whichever is there; Claude Code installs usually have Node). The patch
# travels as base64 here too, so nothing an admin typed is parsed by the shell.
_PATCH_SH = r"""#!/bin/sh
# ============================================================
#  __TITLE__
#  Settings patch for Claude Code: __NAME__
#
#  Sets a few environment variables in the "env" block of
#  ~/.claude/settings.json. Nothing else in the file is changed, and
#  the previous file is saved next to it as settings.json.bak. The
#  variables are listed on your gateway dashboard.
#
#  Run it with:  sh __FILE__
# ============================================================
echo
echo " __TITLE__ - Claude Code settings patch"
echo " __NAME__"
echo
if command -v node >/dev/null 2>&1; then
node - <<'GW_PATCH_NODE'
try {
__NODE_APPLY__
} catch (e) { console.error('   [ERR] ' + e.message); process.exit(1); }
GW_PATCH_NODE
RC=$?
elif command -v python3 >/dev/null 2>&1; then
python3 - <<'GW_PATCH_PYTHON'
__PYTHON_APPLY__
GW_PATCH_PYTHON
RC=$?
else
    echo "   [ERR] This script needs Node.js or Python 3, and found neither." >&2
    RC=1
fi
echo
if [ "$RC" -eq 0 ]; then
    echo "   Done. Restart Claude Code to apply the new settings."
else
    echo "   The patch was not applied. Your settings were not changed."
fi
exit $RC
"""

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
__NODE_APPLY__
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
        _PATCH_BAT.replace("__NODE_APPLY__", _NODE_APPLY).replace("__PATCH_B64__", payload)
        .replace("__TITLE__", _bat_text(APP_TITLE))
        .replace("__NAME__", _bat_text(name))
    )
    # cmd.exe misreads LF-only files; see web_ui.personalized_claude_installer.
    return script.replace("\r\n", "\n").replace("\n", "\r\n")


def render_patch_sh(name: str, env: dict[str, str | None], filename: str = "claude-code-patch.sh") -> str:
    """The Linux / macOS script that applies ``env`` to the ``env`` block of
    the user's settings.json (``apply_env``)."""
    payload = base64.b64encode(json.dumps(env, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = (
        _PATCH_SH.replace("__NODE_APPLY__", _NODE_APPLY).replace("__PYTHON_APPLY__", _PYTHON_APPLY)
        .replace("__PATCH_B64__", payload)
        .replace("__TITLE__", _bat_text(APP_TITLE))
        .replace("__NAME__", _bat_text(name))
        .replace("__FILE__", _bat_text(filename))
    )
    return script.replace("\r\n", "\n")


# The downloads a notice offers: extension -> (label, renderer).
PATCH_FORMATS = ("bat", "sh")


def render_patch(fmt: str, name: str, env: dict[str, str | None], filename: str) -> str:
    """The patch script in one of ``PATCH_FORMATS``. Raises ValueError for any other."""
    if fmt == "bat":
        return render_patch_bat(name, env)
    if fmt == "sh":
        return render_patch_sh(name, env, filename)
    raise ValueError(f"Patch format must be one of: {', '.join(PATCH_FORMATS)}.")


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

    @property
    def missing_patch(self) -> bool:
        """On, its fix is a setting, and there is no patch for everyone:
        its accounts are told to change something without being told what.
        A save refuses this; it can only come from an older row."""
        return self.enabled and self.check.needs_patch and not load_env(self.patch)

    @property
    def deprecated(self) -> str:
        """Why the check is on its way out ("" while it is not). A deprecated
        check still records and shows its notice, but never reminds or mails."""
        return self.check.deprecated


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
        if rule.action != "remind" or rule.deprecated:
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

def clean_fields(form: dict[str, Any], check: Check | None = None) -> dict[str, Any]:
    """Validate an admin's form for a check. Returns column values; raises
    ValueError. With ``check`` (a save, as opposed to a test mail of the
    form as it stands), a check whose fix is a setting cannot be turned on
    while its patch for everyone is empty."""
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
    patch = dump_env(parse_env(str(form.get("patch") or "")))
    if check is not None and check.needs_patch and form.get("enabled") and not patch:
        raise ValueError(
            f"{check.id} tells people to change a setting, so it needs a settings patch: "
            "fill in the Everyone box (NAME=value) before turning it on."
        )
    return {
        "enabled": bool(form.get("enabled")), "action": action, "title": title, "message": message,
        "patch": patch,
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


def history(session: Session, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per violation id, over all time: how many accounts ever had one, when
    the last one was seen and how many days ago. For judging when a check
    can be deprecated or retired; includes ids of retired checks."""
    now = now or utcnow()
    rows = session.exec(
        select(ClientViolation.rule_id, func.count(), func.max(ClientViolation.last_seen_at))
        .group_by(ClientViolation.rule_id)
    ).all()
    return {
        rule_id: {"accounts": int(count), "last_seen_at": last, "quiet_days": max((now - last).days, 0)}
        for rule_id, count, last in rows if last is not None
    }


def is_stale(rule: Rule, seen: dict[str, Any] | None) -> bool:
    """An enabled check nobody has tripped for ``STALE_DAYS``: a candidate
    for retirement. One that never had a violation says nothing either way."""
    return bool(rule.enabled and seen and seen["quiet_days"] >= STALE_DAYS)


def is_resolved(violation: ClientViolation) -> bool:
    """The account reported the fix and has not violated since."""
    return violation.resolved_at is not None and violation.resolved_at >= violation.last_seen_at


def resolve(session: Session, rule_id: str, username: str, now: datetime | None = None) -> bool:
    """Record that ``username`` says it applied the fix for ``rule_id``.
    False when it has no such violation. The next violating request reopens
    it (within the hour: ``last_seen_at`` is written at most hourly per worker)."""
    row = session.exec(
        select(ClientViolation).where(ClientViolation.rule_id == rule_id, ClientViolation.username == username)
    ).first()
    if row is None:
        return False
    # Never earlier than the last violation, so one recorded a moment ago
    # by another worker does not reopen it straight away.
    row.resolved_at = max(now or utcnow(), row.last_seen_at)
    session.add(row)
    session.commit()
    return True


def notices_for(session: Session, user: User, now: datetime | None = None) -> list[dict[str, Any]]:
    """What ``user``'s dashboard shows: the enabled checks it tripped inside
    the window and has not reported as fixed, each with the patch meant for
    this account."""
    rows = {
        row.rule_id: row for row in session.exec(
            select(ClientViolation)
            .where(ClientViolation.username == user.username, ClientViolation.last_seen_at >= _active_since(now))
        ).all() if not is_resolved(row)
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


# ── Everything an account has open ──

def open_items(
    session: Session, now: datetime | None = None, usernames: list[str] | None = None,
) -> dict[str, list[tuple[Rule, ClientViolation]]]:
    """Per account, the enabled checks it has an open violation of (inside
    the window, not reported as fixed), in the order of the code. This is
    "what this person still has to change": the dashboard's combined patch
    and the mail are built from it, so one account is told once, about
    everything."""
    enabled = {rule.id: (index, rule) for index, rule in enumerate(rules(session)) if rule.enabled}
    query = select(ClientViolation).where(ClientViolation.last_seen_at >= _active_since(now))
    if usernames is not None:
        if not usernames:
            return {}
        query = query.where(ClientViolation.username.in_(usernames))  # type: ignore[attr-defined]
    grouped: dict[str, list[tuple[int, Rule, ClientViolation]]] = {}
    for row in session.exec(query).all():
        if row.rule_id in enabled and not is_resolved(row):
            index, rule = enabled[row.rule_id]
            grouped.setdefault(row.username, []).append((index, rule, row))
    return {
        username: [(rule, row) for _, rule, row in sorted(entries, key=lambda e: e[0])]
        for username, entries in grouped.items()
    }


def combined_env(rule_list: list[Any], azure: bool, aws: bool) -> dict[str, str | None]:
    """One patch for several checks: each check's patch for this account, in
    the order given (the code's). Where two set the same variable, the later
    check wins; ``env_conflicts`` tells the admin when that happens."""
    env: dict[str, str | None] = {}
    for rule in rule_list:
        env.update(effective_env(rule, azure, aws))
    return env


def env_conflicts(rule_list: list[Rule]) -> list[dict[str, Any]]:
    """Variables that enabled checks set to different values for the same
    kind of account, with the check that wins in a combined patch."""
    found: dict[tuple, dict[str, Any]] = {}
    for audience in AUDIENCES:
        azure, aws = audience_flags(audience)
        setters: dict[str, list[tuple[str, str | None]]] = {}
        for rule in rule_list:
            if rule.enabled:
                for name, value in effective_env(rule, azure, aws).items():
                    setters.setdefault(name, []).append((rule.id, value))
        for name, entries in setters.items():
            if len({value for _, value in entries}) > 1:
                key = (name, tuple(entries))
                found.setdefault(key, {"name": name, "rules": entries, "winner": entries[-1][0], "audiences": []})
                found[key]["audiences"].append(audience)
    return list(found.values())


def combined_for(session: Session, user: User, now: datetime | None = None) -> dict[str, Any] | None:
    """The dashboard's "everything at once": the merged patch of all the
    account's open checks. None with fewer than two (one notice is its own)."""
    items = open_items(session, now, [user.username]).get(user.username, [])
    if len(items) < 2:
        return None
    env = combined_env([rule for rule, _ in items], *audience_of(user))
    return {"count": len(items), "env": env, "patch": env_snippet(env)}


def resolve_all(session: Session, username: str, now: datetime | None = None) -> int:
    """"I've applied everything": resolve every open item of the account.
    Returns how many."""
    now = now or utcnow()
    items = open_items(session, now, [username]).get(username, [])
    for _, row in items:
        row.resolved_at = max(now, row.last_seen_at)
        session.add(row)
    session.commit()
    return len(items)


# ── Mail: one per account, covering everything it has open ──

def render_digest(username: str, rule_list: list[Any], env: dict[str, str | None]) -> tuple[str, str]:
    """``(subject, html)`` of the mail to one account about the checks in
    ``rule_list`` (each needs ``id``, ``title``, ``message``,
    ``email_subject``), with ``env`` as the one patch that covers them all.
    Everything an admin wrote is escaped."""
    public_url = mailer.settings()["public_url"]
    count = len(rule_list)
    if count == 1:
        subject = (rule_list[0].email_subject or f"[{APP_TITLE}] {rule_list[0].title}").strip()
    else:
        subject = f"[{APP_TITLE}] {count} things to change in your Claude Code setup"
    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;line-height:1.6">',
        f"<p>Hi {html.escape(username)},</p>",
    ]
    if count > 1:
        parts.append(
            f"<p>Requests from your account show {count} things to change in your Claude Code setup. "
            + ("You can fix them all in one go with the settings at the end of this mail.</p>" if env else "</p>")
        )
    for number, rule in enumerate(rule_list, 1):
        heading = f"{number}. {rule.title}" if count > 1 else rule.title
        parts.append(f'<p style="font-size:16px;font-weight:600;margin-bottom:4px">{html.escape(heading)}</p>')
        if rule.message:
            parts.append("<p>" + html.escape(rule.message).replace("\n", "<br>") + "</p>")
    if env:
        lead = "Everything above in one block, for" if count > 1 else "Environment settings for"
        parts.append(
            f"<p>{lead} <code>~/.claude/settings.json</code>:</p>"
            '<pre style="background:#f4f4f4;padding:10px;border-radius:4px;font-size:13px">'
            + html.escape(env_snippet(env)) + "</pre>"
        )
    if public_url:
        link = html.escape(f"{public_url}/dashboard", quote=True)
        notice = "these notices" if count > 1 else "this notice"
        what = (f"see {notice}, download one script that applies the settings for you (Windows, Linux or macOS) "
                "and mark them as done") if env else f"see {notice} and mark them as done"
        parts.append(f'<p><a href="{link}">Open your dashboard</a> to {what}.</p>')
    references = ", ".join(html.escape(rule.id) for rule in rule_list)
    # The sender is a system mailbox nobody reads: say so, and say where
    # help is when the admin has set a support link.
    help_line = ""
    support = _support_url(public_url)
    if support:
        help_line = f' For help, use <a href="{html.escape(support, quote=True)}">the support page</a>.'
    parts.append(
        '<p style="color:#888;font-size:12px;border-top:1px solid #e3e6ea;padding-top:10px;margin-top:18px">'
        f"This is an automated message from {html.escape(APP_TITLE)}, sent because of requests from your account. "
        f"Please do not reply to it: replies are not read.{help_line} Reference: {references}.</p></div>"
    )
    return subject, "".join(parts)


def _support_url(public_url: str) -> str:
    """The admin's support link as something a mail can link to: an
    ``http(s)://`` URL as it is, a site-relative one under ``PUBLIC_BASE_URL``
    (nothing without it). Never raises."""
    try:
        url = (config.get_site_links().get("support_bot_url") or "").strip()
    except Exception:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/") and not url.startswith("//") and public_url:
        return public_url + url
    return ""


def render_mail(rule: Any, username: str, env: dict[str, str | None]) -> tuple[str, str]:
    """The mail about one check alone (the admin's test mail)."""
    return render_digest(username, [rule], env)


def mailable(session: Session, rows: list[ClientViolation]) -> list[ClientViolation]:
    """Violations still worth a mail: the account exists, has a mailbox (not
    an app account) and has not reported the fix."""
    names = [v.username for v in rows if not is_app_account(v.username) and not is_resolved(v)]
    if not names:
        return []
    known = set(session.exec(select(User.username).where(User.username.in_(names))).all())  # type: ignore[attr-defined]
    return [v for v in rows if v.username in known and not is_resolved(v)]


def mail_targets(session: Session, rule_id: str, only_unmailed: bool, now: datetime | None = None) -> list[ClientViolation]:
    """Who a manual send from a check reaches: its accounts inside the window
    (nobody for a deprecated or unknown check)."""
    rule = get_rule(session, rule_id)
    if rule is None or rule.deprecated:
        return []
    rows = mailable(session, violations(session, rule_id, now))
    return [v for v in rows if v.mailed_at is None] if only_unmailed else rows


def _send_digests(session: Session, usernames: list[str], now: datetime | None = None, pause_s: float = 0.0) -> tuple[int, int]:
    """Mail each account once about everything it has open (deprecated
    checks left out), ``pause_s`` apart, and stamp the outcome on every
    violation the mail covered. Returns (sent, failed) in accounts."""
    usernames = [name for name in dict.fromkeys(usernames) if not is_app_account(name)]
    if not usernames:
        return 0, 0
    users = {
        u.username: u for u in session.exec(select(User).where(User.username.in_(usernames))).all()  # type: ignore[attr-defined]
    }
    by_account = open_items(session, now, list(users))
    jobs = []
    for username in usernames:
        user = users.get(username)
        items = [(rule, row) for rule, row in by_account.get(username, []) if not rule.deprecated]
        if user is None or not items:
            continue
        rule_list = [rule for rule, _ in items]
        subject, body = render_digest(username, rule_list, combined_env(rule_list, *audience_of(user)))
        jobs.append((username, [row.id for _, row in items], items[0][1].mail_attempted_at, subject, body))
    session.commit()                # nothing is held open while mail is being sent
    sent = failed = 0
    for position, (username, violation_ids, attempted_before, subject, body) in enumerate(jobs):
        stamp = utcnow()
        # Claim the account before sending: a compare-and-set on its first
        # violation, so the job and an admin's click (or two workers) never
        # both mail the same person.
        first = ClientViolation.id == violation_ids[0]
        unchanged = (
            ClientViolation.mail_attempted_at.is_(None) if attempted_before is None  # type: ignore[union-attr]
            else ClientViolation.mail_attempted_at == attempted_before
        )
        claimed = session.execute(
            update(ClientViolation).where(first, unchanged).values(mail_attempted_at=stamp)
        ).rowcount == 1
        session.commit()
        if not claimed:
            continue
        if position and pause_s > 0:
            time.sleep(pause_s)
        ok, error = mailer.send(mailer.address_for(username), subject, body)
        stamp = utcnow()
        values: dict[str, Any] = {"mail_attempted_at": stamp, "mail_error": "" if ok else error}
        if ok:
            values["mailed_at"] = stamp
            sent += 1
        else:
            failed += 1
        session.execute(update(ClientViolation).where(ClientViolation.id.in_(violation_ids)).values(**values))  # type: ignore[union-attr]
        session.commit()
    return sent, failed


def _auto_due(violation: ClientViolation, repeat_days: int, now: datetime) -> bool:
    if violation.mail_attempted_at is None:
        return True
    if violation.mail_attempted_at > now - timedelta(seconds=MAIL_RETRY_S):
        return False
    if violation.mail_error or violation.mailed_at is None:
        return True  # the last attempt failed
    return repeat_days > 0 and violation.mailed_at <= now - timedelta(days=repeat_days)


def is_queued(violation: ClientViolation, now: datetime | None = None) -> bool:
    """An admin asked for this account to be mailed and it has not happened
    yet (a failed attempt waits ``MAIL_RETRY_S`` before the next)."""
    requested = violation.mail_requested_at
    if requested is None or is_resolved(violation):
        return False
    if violation.mailed_at is not None and violation.mailed_at >= requested:
        return False
    return True


def _request_due(violation: ClientViolation, now: datetime) -> bool:
    if not is_queued(violation, now):
        return False
    attempted = violation.mail_attempted_at
    if attempted is not None and attempted >= violation.mail_requested_at:
        return attempted <= now - timedelta(seconds=MAIL_RETRY_S)   # tried since the request, and failed
    return True


def request_mail(session: Session, rule_id: str, only_unmailed: bool = True, now: datetime | None = None) -> int:
    """An admin's click on a check's mail button: queue its accounts (those
    not mailed yet, or all of them). Nothing is sent here; the mail job
    sends the queue in batches. Returns how many were queued."""
    now = now or utcnow()
    targets = mail_targets(session, rule_id, only_unmailed, now)
    for row in targets:
        row.mail_requested_at = now
        session.add(row)
    session.commit()
    return len(targets)


def due_accounts(session: Session, now: datetime | None = None) -> list[str]:
    """Accounts waiting for a mail, in the order they will get it: first
    those an admin asked for (oldest request first), then those due by a
    check's automatic mail. Deprecated checks never make an account due."""
    now = now or utcnow()
    requested: list[tuple[datetime, str]] = []
    automatic: list[str] = []
    for username, items in open_items(session, now).items():
        if is_app_account(username):
            continue
        live = [(rule, row) for rule, row in items if not rule.deprecated]
        asked = [row.mail_requested_at for _, row in live if _request_due(row, now)]
        if asked:
            requested.append((min(asked), username))
        elif any(rule.auto_email and _auto_due(row, rule.email_repeat_days, now) for rule, row in live):
            automatic.append(username)
    ordered = [username for _, username in sorted(requested)] + sorted(automatic)
    if not ordered:
        return []
    known = set(session.exec(select(User.username).where(User.username.in_(ordered))).all())  # type: ignore[attr-defined]
    return [username for username in ordered if username in known]


def send_due(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Send one batch: the first ``mail_pace()["batch"]`` accounts that are
    waiting, a pause apart. ``waiting`` is what is left for the next run."""
    pace = mail_pace()
    due = due_accounts(session, now)
    batch = due[:pace["batch"]]
    sent, failed = _send_digests(session, batch, now, pace["pause_s"])
    return {"sent": sent, "failed": failed, "waiting": len(due) - len(batch)}


def send_batch(bind=None) -> dict[str, int]:
    """One batch, right now, in its own session: run after an admin's click
    so the first mails do not wait for the job's next run. Never raises."""
    try:
        if not mailer.is_enabled():
            return {"sent": 0, "failed": 0, "waiting": 0}
        with Session(bind or engine) as session:
            result = send_due(session)
        logger.info("Client rule mail batch | sent={} failed={} waiting={}", result["sent"], result["failed"], result["waiting"])
        return result
    except Exception as exc:
        logger.warning("Client rule mail batch failed | error={}: {}", type(exc).__name__, exc)
        return {"sent": 0, "failed": 0, "waiting": 0}


def send_rule_mail(rule_id: str, only_unmailed: bool = True, bind=None) -> dict[str, int]:
    """Queue a check's accounts and send the first batch: what an admin's
    click does, in one call. Returns ``{"sent", "failed"}`` of that batch."""
    try:
        with Session(bind or engine) as session:
            if get_rule(session, rule_id) is None or not mailer.is_enabled():
                return {"sent": 0, "failed": 0}
            request_mail(session, rule_id, only_unmailed)
        result = send_batch(bind)
        return {"sent": result["sent"], "failed": result["failed"]}
    except Exception as exc:
        logger.warning("Client rule mail failed | rule={} error={}: {}", rule_id, type(exc).__name__, exc)
        return {"sent": 0, "failed": 0}


def run_auto_mail(session: Session, now: datetime | None = None) -> str:
    """The ``client_rule_mail`` job: one batch per run. An account is due
    when an admin asked for it, or when one of its open checks has
    ``auto_email`` and has not been mailed (or is due again by that check's
    ``email_repeat_days``: weekly, monthly, ...). It gets one mail about
    everything it has open, which stamps all of it, so the shortest repeat
    among its checks sets the pace. Deprecated checks mail nobody."""
    if not mailer.is_enabled():
        return "mail not configured"
    result = send_due(session, now)
    return f"sent={result['sent']} failed={result['failed']} waiting={result['waiting']}"


MAIL_JOB = Job(
    name="client_rule_mail",
    interval=lambda: env_seconds("CLIENT_RULE_MAIL_INTERVAL_S", 600.0),
    run=run_auto_mail,
)
