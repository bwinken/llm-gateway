"""The settings a client can offer an account to choose: ``GET /v1/settings``.

A terminal tool asks its user "do you want to change this setting?" and needs
to know which settings there are, which value the gateway suggests and what
else may be picked. Each field is one variable of the ``env`` block of Claude
Code's settings.json, with a ``default`` and its ``options``; both depend on
the account (its cloud access decides the models it can pick and which
variant of a patch it gets).

Nothing here is a second source of truth:

- the suggested value of a variable is what the client checks already tell
  this kind of account to set (``client_rules.recommended_env``: the
  settings every account should have, then the patch of every check that is
  on), so the admin's *Clients* tab stays the one place a value is decided;
- the models to pick from are the ones the account sees everywhere else
  (``model_catalog.visible_models``), in the one display order.

What is code, here: which variables take a model (``SETTINGS``, kind
``model``), the other values a switch may have, and the wording. A variable
a patch sets that is not listed below is still offered, with the patch's
value as its only option.

No database read (the checks come from the per-worker snapshot) and never a
key: the variables that hold one are left out whatever a patch says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core import config
from app.models.schema import User
from app.services import client_checks, client_rules, model_catalog

# What a Messages request can be served by: the models worth offering.
MODEL_TYPES: tuple[str, ...] = ("llm", "vlm")

# Never offered: a key is given to a client only for a sign-in.
_KEY_VARIABLES = frozenset((client_rules.API_KEY_ENV, *client_rules.API_KEY_ENV_RETIRED))


@dataclass(frozen=True)
class Setting:
    name: str                                   # the variable in settings.json's env block
    label: str                                  # what the user is asked about
    description: str = ""
    # "model": pick one of the account's models; listed even when no check
    # names it (Claude Code's own default is a model this gateway lacks).
    # "choice": one of ``choices``; listed only when a value is suggested.
    kind: str = "choice"
    choices: tuple[tuple[str, str], ...] = ()   # (value, label), for "choice"


SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="ANTHROPIC_MODEL",
        label="Default model",
        description="The model Claude Code starts a session with.",
        kind="model",
    ),
    Setting(
        name="ANTHROPIC_SMALL_FAST_MODEL",
        label="Background model",
        description=(
            "The model for Claude Code's background tasks (titles, summaries). A small, cheap one is enough."
        ),
        kind="model",
    ),
    Setting(
        name="CLAUDE_CODE_AUTO_MODE_MODEL",
        label="Auto mode model",
        description=(
            "The model that decides which actions auto mode may run unattended "
            f"(Claude Code {client_checks.MIN_CLAUDE_CODE_VERSION} or later). Pick a capable one."
        ),
        kind="model",
    ),
    Setting(
        name="CLAUDE_CODE_AUTO_MODE_SERVER",
        label="Auto mode safety check",
        description=(
            "No model behind this gateway reviews auto mode's actions, so Claude Code has to run that "
            "check itself, as ordinary requests."
        ),
        choices=(("0", "Run by Claude Code"),),
    ),
    Setting(
        name="CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
        label="Model list from the gateway",
        description="Fill Claude Code's /model picker with this gateway's models at startup.",
        choices=(("1", "On"), ("0", "Off")),
    ),
    Setting(
        name="ANTHROPIC_BASE_URL",
        label="Gateway address",
        description="Where Claude Code sends its requests.",
    ),
)

if len({setting.name for setting in SETTINGS}) != len(SETTINGS):
    raise RuntimeError("client_settings: a variable is listed twice")


def _model_options(user: User) -> list[dict[str, Any]]:
    """The models ``user`` can pick, as the dashboard lists them."""
    return [
        {"value": row["id"], "label": row.get("display_name") or row["id"], "backend": row["backend"]}
        for row in model_catalog.visible_models(user)
        if row["type"] in MODEL_TYPES
    ]


def _default_model(options: list[dict[str, Any]]) -> str:
    """The model to suggest when no check names one: the gateway's fallback
    LLM (what answers a request for a model it does not have, so what the
    account gets today), else the first model of its list. "" without any."""
    values = [option["value"] for option in options]
    fallback = config.FALLBACK_MAP.get("llm")
    if fallback in values:
        return fallback
    return values[0] if values else ""


def _field(name: str, label: str, description: str, kind: str, default: str, options: list[dict[str, Any]]) -> dict:
    # The default is always one of the options: a suggested model the
    # account does not see in its list (a hidden one) is still offered.
    if all(option["value"] != default for option in options):
        options = [{"value": default, "label": default}, *options]
    field: dict[str, Any] = {"name": name, "label": label}
    if description:
        field["description"] = description
    field.update(type=kind, default=default, options=options)
    return field


def fields_for(user: User) -> list[dict[str, Any]]:
    """What ``GET /v1/settings`` lists for ``user``: one field per variable,
    the ones in ``SETTINGS`` first and in that order, then any other
    variable the checks tell this account to set. Every field has a
    non-empty ``default`` that is one of its ``options``; no value is None.
    """
    azure, aws = client_rules.audience_of(user)
    suggested = {
        name: value
        for name, value in client_rules.recommended_env(azure, aws).items()
        if value and name not in _KEY_VARIABLES
    }
    models = _model_options(user)
    fields: list[dict[str, Any]] = []
    for setting in SETTINGS:
        if setting.kind == "model":
            options = list(models)
            default = suggested.get(setting.name) or _default_model(options)
        else:
            options = [{"value": value, "label": label} for value, label in setting.choices]
            default = suggested.get(setting.name, "")
        if default:
            fields.append(_field(setting.name, setting.label, setting.description, setting.kind, default, options))
    known = {setting.name for setting in SETTINGS}
    fields.extend(
        _field(name, name, "", "choice", value, [])
        for name, value in suggested.items()
        if name not in known
    )
    return fields
