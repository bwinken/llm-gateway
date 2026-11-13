"""The settings a client can offer an account to choose: ``GET /v1/settings``.

A terminal tool that sets Claude Code up for its user either applies the
suggested value of every setting or lets the user pick. For that it needs to
know which settings there are, what each one is for, which value is suggested
and what else may be picked. Each field is one variable of the ``env`` block
of Claude Code's settings.json.

The fields are a JSON file an admin owns: ``client_settings.json`` in
``CONFIG_DIR`` (beside ``config.toml``), written by hand or from the admin
panel's *Client setup* card (``save``). While there is no such file the
shipped ``client_settings.json.example`` is read instead. Nothing here reads
the client checks or their patches.

The file::

    {"fields": [
      {"name": "ANTHROPIC_MODEL", "label": "Default model",
       "description": "The model Claude Code starts a session with.",
       "type": "model",
       "default": {"everyone": "local-llm", "azure": "gpt-5"},
       "options": {"azure": ["local-llm", "gpt-5"]}},
      {"name": "SOME_SWITCH", "label": "...", "description": "...",
       "type": "choice", "default": "1",
       "options": [{"value": "1", "label": "On"}, {"value": "0", "label": "Off"}]}
    ]}

- ``description`` is required: every variable says what it is for.
- ``type`` ``model``: the options are models. Without ``options`` they are
  the chat models the account sees (``model_catalog.visible_models``); with
  it, the listed aliases the account may use, in that order. Without a usable
  ``default`` the gateway's fallback LLM is suggested, else the first option.
- ``type`` ``choice``: ``options`` (a value, or ``{value, label,
  description}``) and ``default`` are required.
- ``default`` and ``options`` may differ per tier. A tier is the account's
  cloud access; write an object keyed by ``everyone`` / ``azure`` / ``aws`` /
  ``both`` instead of the plain value. The most specific key that applies
  wins: ``both`` (Azure and AWS), else ``aws``, else ``azure``, else
  ``everyone``. A field whose options come out empty for an account is not
  offered to it.

The file is re-read when it changes (its mtime), so an edit by hand or by
another worker applies at the next request. A file that does not validate
changes nothing: the last good fields stay in force and the admin card shows
the error. No database read, and never a key: the variables that hold one
are refused as fields.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core import config
from app.core.logger import logger
from app.services import model_catalog
from app.services.client_rules import API_KEY_ENV, API_KEY_ENV_RETIRED

CLIENT = "claude-code"

SETTINGS_PATH: Path = config._config_dir() / "client_settings.json"
EXAMPLE_PATH: Path = config._PROJECT_ROOT / "client_settings.json.example"

# What a Messages request can be served by: the models worth offering.
MODEL_TYPES: tuple[str, ...] = ("llm", "vlm")
KINDS: tuple[str, ...] = ("model", "choice")

# The keys of a per-tier value, least specific first.
TIERS: tuple[str, ...] = ("everyone", "azure", "aws", "both")
# The kinds of account there are, as (name, azure, aws): what the admin's
# preview shows and what a file is validated for.
ACCOUNTS: tuple[tuple[str, bool, bool], ...] = (
    ("local", False, False), ("azure", True, False), ("aws", False, True), ("both", True, True),
)

# Never a field: a key is given to a client only for a sign-in.
_KEY_VARIABLES = frozenset((API_KEY_ENV, *API_KEY_ENV_RETIRED))

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_FIELDS = 50
_MAX_OPTIONS = 100
_MAX_LABEL = 80
_MAX_DESCRIPTION = 500
_MAX_VALUE = 2000
_MAX_BYTES = 256 * 1024
_FIELD_KEYS = frozenset({"name", "label", "description", "type", "default", "options"})
_OPTION_KEYS = frozenset({"value", "label", "description"})


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    description: str
    kind: str                                       # "model" | "choice"
    default: dict[str, str]                         # per tier key; may be empty for "model"
    # Per tier key. "model": a list of aliases, None = the account's models.
    # "choice": a list of {value, label[, description]}.
    options: dict[str, list] | None


# ── The file → fields ──

def _text(value: Any, what: str, limit: int = _MAX_VALUE) -> str:
    """A value as the text an environment variable holds."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty text value.")
    value = value.strip()
    if len(value) > limit or "\n" in value or "\r" in value:
        raise ValueError(f"{what} is too long or spans lines ({limit} characters at most).")
    return value


def _tiered(value: Any, what: str) -> dict[str, Any]:
    """``value`` as ``{tier key: value}``: an object keyed by tier as it is,
    anything else as the value for everyone."""
    if not isinstance(value, dict):
        return {"everyone": value}
    unknown = sorted(set(value) - set(TIERS))
    if unknown or not value:
        raise ValueError(f"{what}: per-tier values are keyed by {', '.join(TIERS)}, not {', '.join(unknown) or 'nothing'}.")
    return dict(value)


def _option(item: Any, what: str) -> dict[str, str]:
    if isinstance(item, dict):
        unknown = sorted(k for k in item if k not in _OPTION_KEYS and not k.startswith("_"))
        if unknown:
            raise ValueError(f"{what}: unknown key {unknown[0]!r} (an option has value, label, description).")
        value = _text(item.get("value"), f"{what}: value")
        option = {"value": value, "label": _text(item.get("label", value), f"{what}: label", _MAX_LABEL)}
        if item.get("description") not in (None, ""):
            option["description"] = _text(item["description"], f"{what}: description", _MAX_DESCRIPTION)
        return option
    value = _text(item, what)
    return {"value": value, "label": value}


def _options(items: Any, kind: str, what: str) -> list:
    if not isinstance(items, list):
        raise ValueError(f"{what} must be a list.")
    if len(items) > _MAX_OPTIONS:
        raise ValueError(f"{what} has too many entries ({_MAX_OPTIONS} at most).")
    if kind == "model":
        found: list = [_text(item, f"{what}: a model alias") for item in items]
        values = found
    else:
        found = [_option(item, what) for item in items]
        values = [option["value"] for option in found]
    if len(set(values)) != len(values):
        raise ValueError(f"{what} lists a value twice.")
    return found


def pick(tiered: dict[str, Any] | None, azure: bool, aws: bool) -> Any:
    """The value an account with this cloud access gets from a per-tier
    value: the most specific key that applies (``both``, else ``aws``, else
    ``azure``, else ``everyone``). None when no key applies."""
    if not tiered:
        return None
    value = tiered.get("everyone")
    if azure and "azure" in tiered:
        value = tiered["azure"]
    if aws and "aws" in tiered:
        value = tiered["aws"]
    if azure and aws and "both" in tiered:
        value = tiered["both"]
    return value


def _field(raw: Any, index: int) -> Field:
    if not isinstance(raw, dict):
        raise ValueError(f"Field {index + 1} must be an object.")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"Field {index + 1}: \"name\" must be an environment variable name.")
    if name in _KEY_VARIABLES:
        raise ValueError(f"{name} holds the API key and cannot be a field: a client gets the key by signing in.")
    unknown = sorted(k for k in raw if k not in _FIELD_KEYS and not k.startswith("_"))
    if unknown:
        raise ValueError(f"{name}: unknown key {unknown[0]!r} (a field has {', '.join(sorted(_FIELD_KEYS))}).")
    kind = raw.get("type")
    if kind not in KINDS:
        raise ValueError(f"{name}: \"type\" must be one of {', '.join(KINDS)}.")
    label = _text(raw.get("label", name), f"{name}: label", _MAX_LABEL)
    if raw.get("description") in (None, ""):
        raise ValueError(f"{name}: every field needs a \"description\" saying what the variable is for.")
    description = _text(raw["description"], f"{name}: description", _MAX_DESCRIPTION)

    default: dict[str, str] = {}
    if raw.get("default") is not None:
        default = {
            tier: _text(value, f"{name}: default ({tier})")
            for tier, value in _tiered(raw["default"], f"{name}: default").items()
        }
    options: dict[str, list] | None = None
    if raw.get("options") is not None:
        options = {
            tier: _options(items, kind, f"{name}: options ({tier})")
            for tier, items in _tiered(raw["options"], f"{name}: options").items()
        }

    if kind == "choice":
        if options is None:
            raise ValueError(f"{name}: a choice field needs \"options\".")
        for account, azure, aws in ACCOUNTS:
            offered = pick(options, azure, aws)
            if not offered:
                continue  # not offered to this kind of account
            chosen = pick(default, azure, aws)
            if chosen is None:
                raise ValueError(f"{name}: no \"default\" for {account} accounts.")
            if chosen not in [option["value"] for option in offered]:
                raise ValueError(f"{name}: the default for {account} accounts ({chosen!r}) is not one of their options.")
    return Field(name=name, label=label, description=description, kind=kind, default=default, options=options)


def parse(text: str) -> tuple[Field, ...]:
    """A settings file as fields. Raises ValueError saying what is wrong."""
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("The settings file is too large.")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"Not valid JSON: {exc}.")
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
        raise ValueError("The file must be an object with a \"fields\" list.")
    unknown = sorted(k for k in data if k != "fields" and not k.startswith("_"))
    if unknown:
        raise ValueError(f"Unknown top-level key {unknown[0]!r} (only \"fields\").")
    if len(data["fields"]) > _MAX_FIELDS:
        raise ValueError(f"Too many fields ({_MAX_FIELDS} at most).")
    fields = tuple(_field(raw, index) for index, raw in enumerate(data["fields"]))
    names = [field.name for field in fields]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ValueError(f"{twice[0]} is listed twice.")
    return fields


# ── The file on disk (per worker, re-read when it changes) ──

_lock = threading.Lock()
_loaded_key: tuple | None = None
_loaded: tuple[Field, ...] = ()
_error: str = ""


def _source() -> Path:
    return SETTINGS_PATH if SETTINGS_PATH.exists() else EXAMPLE_PATH


def _read() -> tuple[tuple, str | None]:
    path = _source()
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size), path.read_text(encoding="utf-8")
    except OSError as exc:
        return (str(path), type(exc).__name__), None


def load() -> tuple[Field, ...]:
    """The fields in force. Never raises: a file that cannot be read or does
    not validate leaves the last good fields in place (none before the first
    good read) and is reported by ``status``."""
    global _loaded_key, _loaded, _error
    key, text = _read()
    if key == _loaded_key:
        return _loaded
    with _lock:
        if key == _loaded_key:
            return _loaded
        try:
            if text is None:
                raise ValueError("the file cannot be read")
            _loaded, _error = parse(text), ""
        except ValueError as exc:
            _error = str(exc)
            logger.warning("Client settings file ignored | path={} error={}", key[0], exc)
        _loaded_key = key
        return _loaded


def reset_cache() -> None:
    """Forget what was read (tests)."""
    global _loaded_key, _loaded, _error
    with _lock:
        _loaded_key, _loaded, _error = None, (), ""


# ── What one account is offered ──

def _resolve(field: Field, azure: bool, aws: bool, listed: list[dict], known: dict[str, dict]) -> dict | None:
    default = pick(field.default, azure, aws)
    if field.kind == "model":
        wanted = pick(field.options, azure, aws)
        options = list(listed) if wanted is None else [known[alias] for alias in wanted if alias in known]
        values = [option["value"] for option in options]
        if default not in known:
            # No model named for this account, or one it cannot use: what
            # answers a request for a model the gateway lacks, else the first.
            fallback = config.FALLBACK_MAP.get("llm")
            default = fallback if fallback in values else (values[0] if values else None)
        elif default not in values:
            # Suggested but not listed (a hidden model): still offered.
            options.insert(0, known[default])
    else:
        options = [dict(option) for option in pick(field.options, azure, aws) or []]
    if not options or default is None:
        return None
    return {
        "name": field.name, "label": field.label, "description": field.description,
        "type": field.kind, "default": default, "options": options,
    }


def _models(user: Any) -> tuple[list[dict], dict[str, dict]]:
    """``(listed, known)``: the chat models the account sees, in the display
    order, and every chat model it may use by alias (hidden ones too)."""
    def option(row: dict) -> dict:
        return {"value": row["id"], "label": row.get("display_name") or row["id"], "backend": row["backend"]}

    listed = [option(row) for row in model_catalog.visible_models(user) if row["type"] in MODEL_TYPES]
    known = {
        row["id"]: option(row)
        for row in model_catalog.usable_models(user) if row["type"] in MODEL_TYPES
    }
    return listed, known


def fields_for(user: Any) -> list[dict[str, Any]]:
    """What ``GET /v1/settings`` lists for ``user``, in the file's order.
    Every field has a description and a non-empty ``default`` that is one of
    its ``options``; no value is None."""
    hidden = model_catalog.hidden_cloud_backends(user)
    azure, aws = "azure" not in hidden, "bedrock" not in hidden
    listed, known = _models(user)
    resolved = (_resolve(field, azure, aws, listed, known) for field in load())
    return [field for field in resolved if field is not None]


# ── Admin: the file as text, saving it, what each tier gets ──

def _account(azure: bool, aws: bool) -> SimpleNamespace:
    return SimpleNamespace(can_use_azure=azure, can_use_bedrock=aws, is_admin=False)


def preview() -> list[dict[str, Any]]:
    """Per kind of account (``ACCOUNTS``), the fields it is offered."""
    return [
        {"account": name, "fields": fields_for(_account(azure, aws))}
        for name, azure, aws in ACCOUNTS
    ]


def unknown_models(fields: tuple[Field, ...] | None = None) -> list[str]:
    """Model aliases the fields name that no chat model of the gateway has
    (a typo, or a model since removed): such a default or option is skipped
    for every account, so the admin is told."""
    known = set(_models(_account(True, True))[1])
    named: list[str] = []
    for field in (load() if fields is None else fields):
        if field.kind != "model":
            continue
        named.extend(field.default.values())
        for aliases in (field.options or {}).values():
            named.extend(aliases)
    return sorted({alias for alias in named if alias not in known})


def status() -> dict[str, Any]:
    """For the admin card: the file's text, where it came from and whether
    it is in force."""
    fields = load()
    path = _source()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return {
        "text": text,
        "customized": path == SETTINGS_PATH,
        "path": str(SETTINGS_PATH),
        "error": _error,
        "fields": len(fields),
        "names": [field.name for field in fields],   # the file's order, for the preview
        "unknown_models": unknown_models(fields),
        "preview": preview(),
    }


def save(text: str) -> tuple[Field, ...]:
    """Validate ``text`` and make it the settings file. Raises ValueError
    (nothing is written). Replaces the file atomically, as ``save_config``
    does: a temp file in the same directory, then ``os.replace``."""
    fields = parse(text)
    if not text.endswith("\n"):
        text += "\n"
    directory = SETTINGS_PATH.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=directory, prefix=".client_settings.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(text)
            os.replace(temp, SETTINGS_PATH)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ValueError(f"The settings file could not be written ({type(exc).__name__}: {exc}).")
    return fields


def reset() -> None:
    """Remove the settings file: the shipped example is read again."""
    try:
        SETTINGS_PATH.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"The settings file could not be removed ({type(exc).__name__}: {exc}).")
