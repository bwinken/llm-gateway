"""The client checks: what the gateway looks for in a Messages request.

Every check is code, in ``CHECKS`` below, with a violation id that never
changes (``CC001``, ``CC002``, ...). When a request trips an enabled check,
``client_rules`` records a violation for the account under that id
(``client_violations``) and the account gets the check's notice, settings
patch, reminder and mail.

Adding a check:

1. Write its ``detect`` function: it gets the ``Facts`` of one request and
   returns True for a violation. It runs on every Messages request, so no
   I/O and nothing slow; read config through ``app.core.config`` getters.
2. Append a ``Check`` with the next unused id. Ids are never reused.
3. Add a test to ``tests/test_client_rules.py::TestChecks``.

Retiring a check: set ``retired`` to the reason. It is then no longer
evaluated or shown, its id stays taken, and its recorded violations stay in
the database. Don't delete the entry.

What an admin can change per check (admin panel → Client rules, stored in
``client_rule_settings``) is everything except the detection: on / off, the
action, the wording, the settings patch, mail, and the one optional
parameter a check declares (``param_label``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core import config


@dataclass(frozen=True)
class Facts:
    """What a check may look at. ``body`` is the parsed request body: read
    whether a field is there or its plain routing fields (``model``), never
    the conversation."""
    body: dict[str, Any]
    model: str          # body["model"] when it is a string, else ""
    version: str        # Claude Code version from the User-Agent, "" for other clients
    scheme: str         # "http" or "https", as the client connected
    azure_access: bool  # the account may use Azure models (admins: always)
    aws_access: bool    # the account may use AWS Bedrock models (admins: always)
    param: str          # the check's parameter as the admin set it (or its default)


@dataclass(frozen=True)
class Check:
    id: str                              # the violation id; never reused
    name: str                            # short label for the admin
    description: str                     # what it finds and why it matters, for the admin
    detect: Callable[[Facts], bool]
    title: str                           # default notice title (what the user reads)
    message: str                         # default notice text
    # Default settings patch, as NAME=value lines (see client_rules.parse_env):
    # for everyone, and what Azure / AWS accounts get on top.
    env: str = ""
    env_azure: str = ""
    env_aws: str = ""
    enabled: bool = False                # on before an admin has touched it?
    param_label: str = ""                # set when the check takes a parameter
    param_help: str = ""
    param_default: str = ""
    param_pattern: str = ""              # a regex a non-empty parameter must match
    retired: str = ""                    # non-empty: why it was retired


def version_key(version: str) -> tuple[int, ...]:
    """``"2.1.302"`` → ``(2, 1, 302)``, for ordering; ``()`` if it is not one."""
    try:
        return tuple(int(part) for part in version.strip().lstrip("vV").split("."))
    except ValueError:
        return ()


def _names(text: str) -> set[str]:
    return {name for name in re.split(r"[,\s]+", text or "") if name}


# ── Detection ──

def _auto_mode_server_review(f: Facts) -> bool:
    # Claude Code attaches this field when auto mode expects the upstream to
    # review each action. No backend here does, and the field is dropped.
    return "safeguards" in f.body


def _plain_http(f: Facts) -> bool:
    return f.scheme == "http"


def _outdated_claude_code(f: Facts) -> bool:
    minimum, have = version_key(f.param), version_key(f.version)
    return bool(minimum and have and have < minimum)


def _unknown_model(f: Facts) -> bool:
    if not f.model:
        return False
    return (
        f.model not in config.get_model_routing_snapshot()
        and f.model not in config.get_azure_models_snapshot()
        and f.model not in config.get_bedrock_models_snapshot()
    )


def _cloud_model_without_access(f: Facts) -> bool:
    if not f.model:
        return False
    if f.model in config.get_azure_models_snapshot():
        return not f.azure_access
    if f.model in config.get_bedrock_models_snapshot():
        return not f.aws_access
    return False


def _retiring_model(f: Facts) -> bool:
    return bool(f.model) and f.model in _names(f.param)


# ── The checks ──

CHECKS: tuple[Check, ...] = (
    Check(
        id="CC001",
        name="Auto mode waits for a server-side check",
        description=(
            "Claude Code's auto mode asks the upstream to review each action and waits for its verdict. "
            "No model behind this gateway gives one, so auto mode refuses every action. The fix is on the "
            "client: run the check itself, on a model named in the patch."
        ),
        detect=_auto_mode_server_review,
        title="Claude Code auto mode needs a setting to work here",
        message=(
            "Your Claude Code asks for a server-side safety check that this gateway's models do not provide, "
            "so auto mode refuses every action (\"Auto mode is unavailable: its safety check does not run "
            "for this session\").\n"
            "Apply the settings below and restart Claude Code: it then runs the check itself, as ordinary "
            "requests that count toward your usage."
        ),
        env="CLAUDE_CODE_AUTO_MODE_SERVER=0",
        enabled=True,
    ),
    Check(
        id="CC002",
        name="Base URL still uses http://",
        description=(
            "The request reached the gateway over http:// instead of https://. Only seen while nginx still "
            "serves the API on port 80; turn that off once nobody is listed here."
        ),
        detect=_plain_http,
        title="Please switch your gateway address to https://",
        message=(
            "Your client still talks to the gateway over http://, which will be turned off. "
            "Apply the setting below (or change the base URL yourself) and restart Claude Code."
        ),
    ),
    Check(
        id="CC003",
        name="Claude Code is out of date",
        description="Claude Code is older than the minimum version below. Other clients are never listed.",
        detect=_outdated_claude_code,
        title="Please update Claude Code",
        message="Your Claude Code is older than the version this gateway expects. Run the installer again to update it.",
        param_label="Minimum version",
        param_help="Claude Code versions lower than this are listed, e.g. 2.1.302.",
        param_default="2.1.302",
        param_pattern=r"^[vV]?\d+(\.\d+){0,3}$",
    ),
    Check(
        id="CC004",
        name="Asks for a model the gateway does not have",
        description=(
            "The requested model name is not configured here, so the gateway silently answers with a fallback "
            "model. Typical cause: Claude Code's background tasks ask for a default Claude model. Name a "
            "gateway model for them in the patch (ANTHROPIC_SMALL_FAST_MODEL)."
        ),
        detect=_unknown_model,
        title="Claude Code is asking for a model this gateway does not have",
        message=(
            "Some of your requests name a model that is not available here, so another model answers them "
            "without telling you. Apply the settings below so those requests name a model the gateway has."
        ),
    ),
    Check(
        id="CC005",
        name="Asks for a cloud model without access",
        description=(
            "The account asks for an Azure or AWS model it has no access to. The gateway answers with a local "
            "model instead, so the user may believe they are using the cloud model."
        ),
        detect=_cloud_model_without_access,
        title="Your account has no access to the cloud model you selected",
        message=(
            "You selected a cloud model your account cannot use, so a local model answers instead. "
            "Pick a model from your dashboard's list, or ask an administrator for cloud access."
        ),
    ),
    Check(
        id="CC006",
        name="Uses a model that is being retired",
        description="The requested model is one of the names below. Lists nobody until you name at least one.",
        detect=_retiring_model,
        title="The model you use is being retired",
        message="A model you use will be removed from this gateway. Apply the settings below to move to its replacement.",
        param_label="Models being retired",
        param_help="Model names, comma-separated.",
    ),
)

_ID = re.compile(r"^CC\d{3}$")
_seen: set[str] = set()
for _check in CHECKS:
    if not _ID.match(_check.id) or _check.id in _seen:
        raise RuntimeError(f"client_checks: bad or duplicate check id {_check.id!r}")
    _seen.add(_check.id)

BY_ID: dict[str, Check] = {check.id: check for check in CHECKS}
ACTIVE: tuple[Check, ...] = tuple(check for check in CHECKS if not check.retired)
