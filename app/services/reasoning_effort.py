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
    # where a level this model dropped should land instead:
    reasoning_effort_map = { high = "xhigh" }

Policy, applied only when ``reasoning_efforts`` is declared (absent = the
gateway's longstanding faithful pass-through, unchanged):

* an accepted level passes through untouched;
* anything else goes to ``reasoning_effort_map``, which is the only place a
  level is ever rewritten into another one;
* a level the map does not name is **dropped** — the field is removed and
  the downstream applies its own default.

Nothing is guessed at: the gateway either sends what the caller asked for,
sends what the operator said to send instead, or sends nothing. There is
deliberately no "nearest level" rule — a rewrite the operator did not write
down would quietly change how much reasoning (and money) a request buys.

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

# The level vocabulary, weakest -> strongest. Nothing here decides policy —
# the gateway never reasons about distance between levels — but it is the
# set the admin UI offers and the order it lists them in.
EFFORT_LEVELS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

# Spelling variants seen in the wild. `xhigh` is written three ways; `max`
# is NOT one of them — it is a level of its own, one step above xhigh, on
# both ladders the gateway sits between (Claude Code offers low / medium /
# high / xhigh / max, and GPT-5.6 accepts none / low / medium / high /
# xhigh / max on the Responses API). Folding it onto xhigh would silently
# sell the caller the second-strongest setting when they paid for the
# strongest — exactly the substitution this module exists to refuse.
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
    "max": "max",
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


def canonical_effort(value: Any) -> Any:
    """Fold a known spelling variant onto its canonical name; else verbatim.

    ``extra-high`` / ``x-high`` / ``extra_high`` are all ``xhigh``. For
    translators that have to *interpret* the level rather than forward it —
    Converse expands it into a Claude thinking budget, so a spelling its
    bucket table has never seen would land on the *medium* default and
    quietly buy less thinking than ``xhigh``. Forwarding paths do not call
    this: on a route that declares nothing the gateway sends the client's
    string as it arrived.

    Only spellings this module knows are touched, and ``max`` is a level,
    not a spelling of one (see EFFORT_ALIASES). An unknown level, or a
    non-string, comes back exactly as it arrived — the downstream is the
    authority on what it means, and folding is not clamping: no level is
    ever traded for a different one here.
    """
    if not isinstance(value, str):
        return value
    return EFFORT_ALIASES.get(value.strip().lower(), value)


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


def adapt_effort(
    effort: Any, route: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Resolve one requested effort against a route's declaration.

    Returns ``(value, note)``: ``value`` is what to send (None = drop the
    field), ``note`` is a short "high -> medium" description when the value
    changed, for logging. A route with no declaration returns the request
    untouched and no note.

    Order matters: an accepted level is never touched, so a map entry only
    ever applies to a level this model does not accept.
    """
    requested = normalize_effort(effort)
    if requested is None:
        return None, None

    supported = declared_efforts(route)
    if supported is None:
        # Nothing declared — faithful pass-through, as before this feature.
        return requested, None
    if requested in supported:
        return requested, None

    # Not accepted: the operator's map is the only thing that can turn it
    # into another level. A target outside `reasoning_efforts` is honored as
    # written — the operator is the authority on their own downstream.
    override = _declared_map(route)
    if requested in override:
        target = override[requested] or None
        if target is None:
            return None, f"{requested} -> dropped (reasoning_effort_map)"
        return target, f"{requested} -> {target} (reasoning_effort_map)"

    return None, f"{requested} -> dropped (not accepted, unmapped)"


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

    A route with no declaration is left strictly alone — the spelling
    included, and including a level this module wouldn't recognize. What a
    downstream accepts is the operator's to write down, so an undeclared
    route is byte-faithful: the only rewrites are the ones config asked
    for. (Once a declaration exists, the operator's own vocabulary is
    normalized against the request's — ``adapt_effort`` de-aliases both
    sides, so accepting ``xhigh`` accepts ``extra-high`` too.)
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
