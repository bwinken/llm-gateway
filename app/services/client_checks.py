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

A check's life, all of it decided here in code:

1. **Active**: detected, recorded, the account is told (notice, and the
   reminder / mail the admin chose).
2. **Deprecated** (set ``deprecated`` to the reason, and what replaces it if
   anything): still detected and recorded, and the account still sees the
   notice, but nobody is interrupted or mailed any more. This is the quiet
   period for watching who is left; the admin page shows when each check
   last had a violation and suggests retiring one that has been silent for
   ``client_rules.STALE_DAYS``.
3. **Retired** (set ``retired`` to the reason): no longer evaluated; the
   admin page lists it under "Retired" with the reason. Its id stays taken
   and its recorded violations stay in the database. Don't delete the entry.

Everything a check decides with is here, in code: thresholds such as
``MIN_CLAUDE_CODE_VERSION`` are constants in this file, not settings, so
the database never holds any part of a rule's judgment. It holds the
violations (by id) and, per check, how its accounts are told (admin panel →
Client rules, ``client_rule_settings``): on / off, the action, the wording,
the settings patch and mail.
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
    deprecated: str = ""                 # non-empty: why it is on its way out (no reminder, no mail)
    retired: str = ""                    # non-empty: why it was retired (not evaluated at all)


def version_key(version: str) -> tuple[int, ...]:
    """``"2.1.302"`` → ``(2, 1, 302)``, for ordering; ``()`` if it is not one."""
    try:
        return tuple(int(part) for part in version.strip().lstrip("vV").split("."))
    except ValueError:
        return ()


# ── Thresholds (part of the judgment, so they live here) ──

# CC003: Claude Code older than this is out of date. 2.1.302 is the first
# version that honors CLAUDE_CODE_AUTO_MODE_MODEL.
MIN_CLAUDE_CODE_VERSION = "2.1.302"


# ── Detection ──

def _auto_mode_server_review(f: Facts) -> bool:
    # Claude Code attaches this field when auto mode expects the upstream to
    # review each action. No backend here does, and the field is dropped.
    return "safeguards" in f.body


def _plain_http(f: Facts) -> bool:
    return f.scheme == "http"


def _outdated_claude_code(f: Facts) -> bool:
    have = version_key(f.version)
    return bool(have) and have < version_key(MIN_CLAUDE_CODE_VERSION)


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
        description=(
            f"Claude Code is older than {MIN_CLAUDE_CODE_VERSION} (MIN_CLAUDE_CODE_VERSION in client_checks.py). "
            "Other clients are never listed."
        ),
        detect=_outdated_claude_code,
        title="Please update Claude Code",
        message=(
            f"Your Claude Code is older than {MIN_CLAUDE_CODE_VERSION}, the version this gateway expects. "
            "Run the installer again to update it."
        ),
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
)

_ID = re.compile(r"^CC\d{3}$")
_seen: set[str] = set()
for _check in CHECKS:
    if not _ID.match(_check.id) or _check.id in _seen:
        raise RuntimeError(f"client_checks: bad or duplicate check id {_check.id!r}")
    _seen.add(_check.id)

BY_ID: dict[str, Check] = {check.id: check for check in CHECKS}
ACTIVE: tuple[Check, ...] = tuple(check for check in CHECKS if not check.retired)
RETIRED: tuple[Check, ...] = tuple(check for check in CHECKS if check.retired)
