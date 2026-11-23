"""The settings patch: environment variables for the ``env`` block of
``~/.claude/settings.json``, model names to replace, and the scripts that
apply them.

A patch is text an admin writes (``NAME=value`` to set, ``-NAME`` to remove,
``NAME=@setup`` for the value Client setup suggests the account) and nothing
else: ``parse_env`` cannot express a change outside the ``env``
block, and every script does exactly ``apply_env`` (Node for the ``.bat`` and
the ``.sh``, Python as the ``.sh`` fallback, PowerShell as the ``.bat``'s).
The patch reaches the user's machine as base64 inside the script, never as
interpolated text.

A script may also carry **model renames** (``old-model -> new-model``, for a
model that is being replaced: ``parse_renames``, ``apply_renames``). A
rename changes a value only where it is exactly the old name, and only in
the two places settings.json names a model: the ``model`` setting and the
``env`` variables with ``MODEL`` in their name. It never adds a setting, so
a user who chose another model keeps it. Renames are applied before the
variables are set.

This module knows no check, no account and no database. Which variables an
account gets is ``client_rules.effective_env`` / ``combined_env`` (the
notices' patch), which renames ``model_changes.renames_for`` (the
model-change script, a download of its own); the API key patch
(``key_env``) is the one patch that is about neither.
"""

from __future__ import annotations

import base64
import json
import re

from app.core.config import APP_TITLE


# The settings patch only ever touches the ``env`` block of settings.json.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_ENV_MAX_VARS = 50


_ENV_MAX_VALUE = 2000

# A value an admin writes instead of a literal one (``NAME=@setup``): "what
# Client setup suggests this account for NAME". It is stored as written and
# replaced by ``client_rules.effective_env``, so the value has one home (the
# admin's ``client_settings.json``) and no script ever receives the marker.
FROM_SETUP = "@setup"


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


# ── Model renames (a model that is being replaced) ──

# A model name as an admin may write it in a rename: an alias of this
# gateway, so no spaces, quotes or brackets (a bracket starts Claude Code's
# own suffix, as in ``name[1m]``).
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")


_RENAME_MAX = 20


_RENAME_ARROWS = ("->", "=>", "\u2192", "=")


def parse_renames(text: str | None) -> dict[str, str]:
    """An admin's rename text as ``{old: new}``: one ``old-model -> new-model``
    per line (``=>``, ``\u2192`` or ``=`` work too), ``#`` comments. Raises
    ValueError."""
    renames: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for arrow in _RENAME_ARROWS:
            old, sep, new = line.partition(arrow)
            if sep:
                break
        else:
            raise ValueError(f"Model change: write old-model -> new-model, not {line[:40]!r}.")
        old, new = old.strip(), new.strip()
        for name in (old, new):
            if not _MODEL_NAME.match(name):
                raise ValueError(f"Model change: {name[:40]!r} is not a model name.")
        if old == new:
            raise ValueError(f"Model change: {old} is replaced by itself.")
        if old in renames and renames[old] != new:
            raise ValueError(f"Model change: {old} is listed twice, with different replacements.")
        renames[old] = new
    if len(renames) > _RENAME_MAX:
        raise ValueError(f"Model change: too many models ({_RENAME_MAX} at most).")
    return renames


def dump_renames(renames: dict[str, str]) -> str:
    """How renames are stored: JSON, "" when empty."""
    return json.dumps(renames, ensure_ascii=False) if renames else ""


def load_renames(stored: str | None) -> dict[str, str]:
    """Stored renames back as a dict ({} for anything unreadable)."""
    try:
        data = json.loads(stored) if stored else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(old): str(new) for old, new in data.items()
        if _MODEL_NAME.match(str(old)) and isinstance(new, str) and _MODEL_NAME.match(new)
    }


def rename_lines(renames: dict[str, str]) -> str:
    """Renames as the admin edits them: ``old -> new`` lines."""
    return "\n".join(f"{old} -> {new}" for old, new in renames.items())


def is_model_variable(name: str) -> bool:
    """An ``env`` variable that holds a model name, by its own name
    (``ANTHROPIC_MODEL``, ``ANTHROPIC_SMALL_FAST_MODEL``,
    ``CLAUDE_CODE_SUBAGENT_MODEL``, ...)."""
    return "MODEL" in name.upper()


def renamed(value: object, renames: dict[str, str]) -> object:
    """``value`` with a replaced model's name changed to the new one. Only a
    value that is exactly the old name, or the old name with Claude Code's
    bracket suffix (``old[1m]``, which is kept). One look-up, never chained:
    with ``a -> b`` and ``b -> c``, ``a`` becomes ``b``."""
    if not isinstance(value, str):
        return value
    if value in renames:
        return renames[value]
    at = value.find("[")
    if at > 0 and value.endswith("]") and value[:at] in renames:
        return renames[value[:at]] + value[at:]
    return value


def apply_renames(settings: dict, renames: dict[str, str]) -> int:
    """What the downloaded script does to settings.json for a replaced
    model, in place: change its name in the ``model`` setting and in the
    ``env`` variables that hold a model (``is_model_variable``), where the
    value is exactly the old name. Nothing is added and nothing else is
    read. Returns how many values changed."""
    changed = 0
    if isinstance(settings.get("model"), str):
        new = renamed(settings["model"], renames)
        changed += new != settings["model"]
        settings["model"] = new
    block = settings.get("env")
    if isinstance(block, dict):
        for name in list(block):
            if is_model_variable(name) and isinstance(block[name], str):
                new = renamed(block[name], renames)
                changed += new != block[name]
                block[name] = new
    return changed


def apply_patch(settings: dict, env: dict[str, str | None], renames: dict[str, str] | None = None) -> dict:
    """Everything a downloaded script does to settings.json, in its order:
    the renames, then the variables (``apply_env``; with none to set or
    remove, a file without an ``env`` block does not get one)."""
    if renames:
        apply_renames(settings, renames)
    if env:
        apply_env(settings, env)
    return settings


# What the downloaded scripts do, once per runtime. Each must do exactly
# ``apply_patch``: read settings.json (a missing or empty file is {}), refuse
# one that is not a JSON object or whose "env" is not an object, save a
# .bak, replace the model names (``apply_renames``), set or remove the
# variables in "env" (``apply_env``) and write the file back. The payload is
# ``{"env": {...}, "models": {old: new}}``.
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
  const env = isObj(patch.env) ? patch.env : {};
  const models = isObj(patch.models) ? patch.models : {};
  const hasEnv = settings.env !== undefined && settings.env !== null;
  if (hasEnv && !isObj(settings.env)) throw new Error('"env" in settings.json is not an object');
  const known = (name) => Object.prototype.hasOwnProperty.call(models, name);
  let replaced = 0;
  const renamed = (v) => {
    let out = v;
    const at = v.indexOf('[');
    if (known(v)) out = String(models[v]);
    else if (at > 0 && v.endsWith(']') && known(v.slice(0, at))) out = String(models[v.slice(0, at)]) + v.slice(at);
    if (out !== v) replaced += 1;
    return out;
  };
  if (typeof settings.model === 'string') settings.model = renamed(settings.model);
  if (hasEnv) {
    for (const k of Object.keys(settings.env)) {
      if (k.toUpperCase().includes('MODEL') && typeof settings.env[k] === 'string') settings.env[k] = renamed(settings.env[k]);
    }
  }
  if (Object.keys(env).length) {
    if (!hasEnv) settings.env = {};
    for (const k of Object.keys(env)) {
      if (env[k] === null) delete settings.env[k];
      else settings.env[k] = String(env[k]);
    }
  }
  fs.writeFileSync(file, JSON.stringify(settings, null, 2) + '\n', 'utf8');
  if (Object.keys(models).length) console.log('   [OK] Model names replaced: ' + replaced);
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
env = patch.get('env') if isinstance(patch.get('env'), dict) else {}
models = patch.get('models') if isinstance(patch.get('models'), dict) else {}
has_env = settings.get('env') is not None
if has_env and not isinstance(settings['env'], dict):
    sys.exit('   [ERR] "env" in settings.json is not an object')
replaced = 0
def renamed(v):
    global replaced
    out = v
    at = v.find('[')
    if v in models:
        out = str(models[v])
    elif at > 0 and v.endswith(']') and v[:at] in models:
        out = str(models[v[:at]]) + v[at:]
    if out != v:
        replaced += 1
    return out
if isinstance(settings.get('model'), str):
    settings['model'] = renamed(settings['model'])
if has_env:
    for name in list(settings['env']):
        if 'MODEL' in name.upper() and isinstance(settings['env'][name], str):
            settings['env'][name] = renamed(settings['env'][name])
if env:
    if not has_env:
        settings['env'] = {}
    for name, value in env.items():
        if value is None:
            settings['env'].pop(name, None)
        else:
            settings['env'][name] = str(value)
with open(path, 'w', encoding='utf-8') as f:
    f.write(json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
if models:
    print('   [OK] Model names replaced: ' + str(replaced))
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
#  ~/.claude/settings.json and, for a model that is being replaced,
#  changes its name where the file names it (the "model" setting and
#  the model variables in "env"). Nothing else in the file is changed,
#  and the previous file is saved next to it as settings.json.bak.
#  What it changes is listed on your gateway dashboard.
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
REM  %USERPROFILE%\.claude\settings.json and, for a model that is being
REM  replaced, changes its name where the file names it (the "model"
REM  setting and the model variables in "env"). Nothing else in the file
REM  is changed, and the previous file is saved next to it as
REM  settings.json.bak. What it changes is listed on your gateway dashboard.
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
    $envPatch = [ordered]@{}
    if ($patch['env'] -is [System.Collections.IDictionary]) { $envPatch = $patch['env'] }
    $models = [ordered]@{}
    if ($patch['models'] -is [System.Collections.IDictionary]) { $models = $patch['models'] }
    $hasEnv = $settings.Contains('env') -and ($null -ne $settings['env'])
    if ($hasEnv -and (-not ($settings['env'] -is [System.Collections.IDictionary]))) { throw '"env" in settings.json is not an object' }
    # The new name of a replaced model, or $null. Compared with -ceq: these
    # tables ignore case, a model name does not.
    function GwNewName([string]$name) {
        foreach ($old in @($models.Keys)) { if ($old -ceq $name) { return [string]$models[$old] } }
        return $null
    }
    function GwRenamed([string]$v) {
        $new = GwNewName $v
        if ($null -ne $new) { return $new }
        $at = $v.IndexOf('[')
        if (($at -gt 0) -and $v.EndsWith(']')) {
            $new = GwNewName ($v.Substring(0, $at))
            if ($null -ne $new) { return ($new + $v.Substring($at)) }
        }
        return $v
    }
    $replaced = 0
    if ($settings.Contains('model') -and ($settings['model'] -is [string])) {
        $new = GwRenamed ($settings['model'])
        if ($new -cne $settings['model']) { $settings['model'] = $new; $replaced++ }
    }
    if ($hasEnv) {
        foreach ($k in @($settings['env'].Keys)) {
            $cur = $settings['env'][$k]
            if (([string]$k).ToUpperInvariant().Contains('MODEL') -and ($cur -is [string])) {
                $new = GwRenamed $cur
                if ($new -cne $cur) { $settings['env'][$k] = $new; $replaced++ }
            }
        }
    }
    if ($envPatch.Count -gt 0) {
        if (-not $hasEnv) { $settings['env'] = [ordered]@{} }
        foreach ($k in @($envPatch.Keys)) {
            $v = $envPatch[$k]
            if ($null -eq $v) {
                if ($settings['env'].Contains($k)) { $settings['env'].Remove($k) }
            } else {
                $settings['env'][$k] = [string]$v
            }
        }
    }
    $json = ConvertTo-Json -InputObject $settings -Depth 100
    [IO.File]::WriteAllText($file, $json + "`r`n", (New-Object System.Text.UTF8Encoding $false))
    if ($models.Count -gt 0) { Write-Host ('   [OK] Model names replaced: ' + $replaced) }
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


def _payload(env: dict[str, str | None], renames: dict[str, str] | None) -> str:
    """What a script carries, as base64: the variables and the renames."""
    data = {"env": env or {}, "models": renames or {}}
    return base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("ascii")


def render_patch_bat(name: str, env: dict[str, str | None], renames: dict[str, str] | None = None) -> str:
    """The Windows script that applies ``env`` and ``renames`` to the user's
    settings.json (``apply_patch``)."""
    payload = _payload(env, renames)
    script = (
        _PATCH_BAT.replace("__NODE_APPLY__", _NODE_APPLY).replace("__PATCH_B64__", payload)
        .replace("__TITLE__", _bat_text(APP_TITLE))
        .replace("__NAME__", _bat_text(name))
    )
    # cmd.exe misreads LF-only files; see dashboard.actions.personalized_claude_installer.
    return script.replace("\r\n", "\n").replace("\n", "\r\n")


def render_patch_sh(
    name: str, env: dict[str, str | None], filename: str = "claude-code-patch.sh",
    renames: dict[str, str] | None = None,
) -> str:
    """The Linux / macOS script that applies ``env`` and ``renames`` to the
    user's settings.json (``apply_patch``)."""
    payload = _payload(env, renames)
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


def render_patch(
    fmt: str, name: str, env: dict[str, str | None], filename: str, renames: dict[str, str] | None = None,
) -> str:
    """The patch script in one of ``PATCH_FORMATS``. Raises ValueError for any other."""
    if fmt == "bat":
        return render_patch_bat(name, env, renames)
    if fmt == "sh":
        return render_patch_sh(name, env, filename, renames)
    raise ValueError(f"Patch format must be one of: {', '.join(PATCH_FORMATS)}.")


# ── The account's API key, as a patch ──

# The one variable Claude Code gets the gateway key in (sent as
# ``Authorization: Bearer``). A regenerated key reaches a user's machine as a
# patch that sets it.
API_KEY_ENV = "ANTHROPIC_AUTH_TOKEN"


# Variables that used to hold the key as well (older installers wrote both).
# The key patch removes them: a second copy would go stale on the next
# regenerate and keep being sent as ``x-api-key``.
API_KEY_ENV_RETIRED = ("ANTHROPIC_API_KEY",)


def key_env(api_key: str) -> dict[str, str | None]:
    """The patch that puts an account's current API key into Claude Code:
    ``API_KEY_ENV`` set to the key, ``API_KEY_ENV_RETIRED`` removed, nothing
    else. It is not a check's patch (no violation, no baseline) and is only
    ever served to the account itself."""
    return {API_KEY_ENV: api_key, **{name: None for name in API_KEY_ENV_RETIRED}}
