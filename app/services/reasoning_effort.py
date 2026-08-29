"""Per-model reasoning-effort compatibility.

Model upgrades move the goalposts on which effort levels a downstream
accepts: a new build may drop ``high`` (or add ``xhigh`` / ``none``) while
the clients pointed at that alias — Claude Code, Roo Code, an internal
script pinned months ago — keep sending the level the *previous* model
took. The downstream then 400s a request that used to work, and the only
thing that actually changed is a string.

This module is the one place that reconciles the two. A model entry in
``config.toml`` declares what its downstream really accepts::

    [models.llm."my-llm"]
    is_reasoning = true
    reasoning_efforts = ["none", "low", "medium", "xhigh"]   # no "high"
    # optional, wins over the nearest-level rule below:
    # reasoning_effort_map = { high = "xhigh" }

Policy, applied only when ``reasoning_efforts`` is declared (absent = the
gateway's longstanding faithful pass-through, unchanged):

* a supported level passes through untouched;
* an unsupported one is remapped — ``reasoning_effort_map`` first, else the
  nearest level on the ladder, **preferring the next one down** so a
  compatibility rewrite never silently buys more reasoning (and more cost)
  than the caller asked for; only when nothing lower exists does it round up;
* an empty list (``reasoning_efforts = []``) or an unknown spelling drops
  the field, leaving the downstream on its own default rather than sending
  a value it is known to reject.

Adaptation happens on the way out, after the route is resolved, so it
covers both shapes an effort level can travel in: OpenAI
``reasoning_effort`` (the gateway's internal pivot, and what the vLLM and
Bedrock paths send) and the Azure Responses ``reasoning.effort`` object.
The Anthropic ``/v1/messages`` surface feeds the first of those through the
translation in ``anthropic_adapter``; the vLLM-native pass-through needs no
pass of its own, since ``sanitize_native_messages_body`` already strips the
non-schema ``effort`` field and ``thinking.budget_tokens`` is a token budget
the downstream clamps for itself, not a level it can reject.
"""

from __future__ import annotations

from typing import Any

from app.core.logger import logger

# Ordered weakest -> strongest. Membership also decides whether a spelling
# can be placed at all: a level that isn't here can't be remapped by
# distance, so it's dropped instead of guessed at.
EFFORT_LADDER: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh",
)

# Spelling variants seen in the wild (Claude Code sends xhigh three ways).
# Only spellings are normalized here — levels are never clamped at parse
# time; that is this module's job, per-model.
EFFORT_ALIASES: dict[str, str] = {
    "minimal": "minimal",
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "max": "xhigh",
}


def normalize_effort(value: Any) -> str | None:
    """Lowercase + de-alias one effort spelling. None for a non-string/blank.

    Unknown spellings come back verbatim (lowercased) — the downstream, or
    the per-model declaration, is the authority on what they mean.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    return EFFORT_ALIASES.get(text, text)


def declared_efforts(route: dict[str, Any] | None) -> list[str] | None:
    """Return the route's supported effort levels, or None when undeclared.

    An empty list is meaningful (the model takes no effort knob) and is
    returned as such; a malformed declaration is treated as undeclared so a
    config typo degrades to today's pass-through instead of dropping every
    caller's effort.
    """
    if not route:
        return None
    raw = route.get("reasoning_efforts")
    if not isinstance(raw, (list, tuple)):
        if raw is not None:
            logger.warning(
                "reasoning_efforts must be a list of effort names, got {} — ignoring",
                type(raw).__name__,
            )
        return None
    out: list[str] = []
    for item in raw:
        level = normalize_effort(item)
        if level and level not in out:
            out.append(level)
    return out


def _declared_map(route: dict[str, Any] | None) -> dict[str, str]:
    """Normalized `reasoning_effort_map` (requested level -> level to send).

    A blank/None target means "drop the field for this level" and is kept as
    an empty string so callers can tell it apart from "no rule".
    """
    if not route:
        return {}
    raw = route.get("reasoning_effort_map")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning(
                "reasoning_effort_map must be a table of effort names, got {} — ignoring",
                type(raw).__name__,
            )
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        src = normalize_effort(key)
        if not src:
            continue
        out[src] = normalize_effort(value) or ""
    return out


def _nearest_supported(level: str, supported: list[str]) -> str | None:
    """Closest supported level to `level`, preferring the next one DOWN.

    Downgrading is the safe direction for an automatic rewrite: less
    reasoning costs the caller less than they budgeted, where an automatic
    upgrade spends tokens (and money) they never asked for. Rounding up
    only happens when the model supports nothing weaker.
    """
    if level not in EFFORT_LADDER:
        return None
    ranked = [lv for lv in EFFORT_LADDER if lv in supported]
    if not ranked:
        return None
    idx = EFFORT_LADDER.index(level)
    lower = [lv for lv in ranked if EFFORT_LADDER.index(lv) < idx]
    if lower:
        return lower[-1]
    return ranked[0]


def adapt_effort(
    effort: Any, route: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Resolve one requested effort against a route's declaration.

    Returns ``(value, note)``: ``value`` is what to send (None = drop the
    field), ``note`` is a short "high -> medium" description when the value
    changed, for logging. A route with no declaration returns the request
    untouched and no note.
    """
    requested = normalize_effort(effort)
    if requested is None:
        return None, None

    supported = declared_efforts(route)
    if supported is None:
        # Nothing declared — faithful pass-through, as before this feature.
        return requested, None

    override = _declared_map(route)
    if requested in override:
        target = override[requested] or None
        if target == requested:
            return requested, None
        return target, f"{requested} -> {target or 'dropped'} (reasoning_effort_map)"

    if not supported:
        return None, f"{requested} -> dropped (model takes no reasoning_effort)"
    if requested in supported:
        return requested, None

    target = _nearest_supported(requested, supported)
    if target is None:
        return None, f"{requested} -> dropped (unknown level, supported={supported})"
    return target, f"{requested} -> {target} (supported={supported})"


def _log(alias: str, endpoint: str, note: str | None) -> None:
    if note:
        logger.info(
            "reasoning_effort adapted | model={} endpoint={} {}", alias, endpoint, note,
        )


def apply_to_openai_body(
    body: dict[str, Any] | None, route: dict[str, Any] | None,
    alias: str = "", endpoint: str = "",
) -> None:
    """Adapt ``body["reasoning_effort"]`` in place (OpenAI chat shape).

    A route with no declaration is left strictly alone — including a value
    this module wouldn't recognize, which stays the downstream's business.
    """
    if not isinstance(body, dict) or "reasoning_effort" not in body:
        return
    if declared_efforts(route) is None:
        return
    value, note = adapt_effort(body.get("reasoning_effort"), route)
    if value is None:
        body.pop("reasoning_effort", None)
    else:
        body["reasoning_effort"] = value
    _log(alias, endpoint, note)


def apply_to_responses_body(
    body: dict[str, Any] | None, route: dict[str, Any] | None,
    alias: str = "", endpoint: str = "",
) -> None:
    """Adapt ``body["reasoning"]["effort"]`` in place (Azure Responses shape).

    The ``/azure/v1/responses`` pass-through otherwise leaves parameters to
    the client; this stays a no-op unless the model declares
    ``reasoning_efforts``, so that contract only bends where an operator has
    said the deployment rejects a level.
    """
    if not isinstance(body, dict) or declared_efforts(route) is None:
        return
    reasoning = body.get("reasoning")
    if not isinstance(reasoning, dict) or "effort" not in reasoning:
        return
    value, note = adapt_effort(reasoning.get("effort"), route)
    if value is None:
        reasoning.pop("effort", None)
        if not reasoning:
            body.pop("reasoning", None)
    else:
        reasoning["effort"] = value
    _log(alias, endpoint, note)
