"""
Rate-limit (429) fallback for the managed cloud backends (Azure, Bedrock).

A cloud deployment that hits its quota answers 429. An entry can name a
sibling on the SAME backend to take the request instead::

    [azure_models.gpt-5]
    rate_limit_fallback = "gpt-5-mini"

The chain is followed hop by hop (``gpt-5 -> gpt-5-mini -> gpt-4o``), each
hop at most once, up to ``_MAX_HOPS`` extra attempts. Only a 429 moves along
the chain; every other status surfaces as-is, and when the last model in the
chain is also rate limited its 429 (with the provider's own body) is what
the client gets. Streams fall back only at pre-flight — the 429 arrives
before anything has been sent to the client, so switching is invisible;
nothing is retried once a stream is flowing.

The fallback never crosses backends: an Azure alias only falls back to an
Azure alias, a Bedrock alias to a Bedrock alias, so the per-backend budget
already checked at auth still applies.
"""

from __future__ import annotations

from typing import Any, Callable

from app.core.logger import logger

# Sentinel returned by a backend's send helper when the downstream answered
# 429 and the caller asked to be told instead of getting the 429 response.
RATE_LIMITED = object()

_MAX_HOPS = 3


def rate_limit_chain(
    alias: str,
    entry: dict[str, Any],
    models: dict[str, dict[str, Any]],
    allowed_types: tuple[str, ...] | list[str],
    is_usable: Callable[[dict[str, Any]], bool],
    backend: str,
) -> list[tuple[str, dict[str, Any]]]:
    """``[(alias, entry), (fallback_alias, fallback_entry), ...]``.

    The first element is always the resolved model itself. A hop is skipped
    (and the chain ends there, with a WARNING) when the target is unknown,
    of a type the endpoint can't serve, missing its connection fields, or
    already in the chain.
    """
    chain: list[tuple[str, dict[str, Any]]] = [(alias, entry)]
    seen = {alias}
    current = entry
    while len(chain) <= _MAX_HOPS:
        target = current.get("rate_limit_fallback")
        if not target or not isinstance(target, str):
            break
        if target in seen:
            logger.warning(
                "{} rate_limit_fallback cycle at '{}' -> '{}' — chain stops",
                backend, chain[-1][0], target,
            )
            break
        target_entry = models.get(target)
        if (
            target_entry is None
            or target_entry.get("type", "llm") not in allowed_types
            or not is_usable(target_entry)
        ):
            logger.warning(
                "{} rate_limit_fallback of '{}' points to '{}', which is not a "
                "usable {} model — chain stops",
                backend, chain[-1][0], target, "/".join(allowed_types),
            )
            break
        chain.append((target, target_entry))
        seen.add(target)
        current = target_entry
    return chain


def note_fallback(
    fallback_reason: str | None,
    backend: str,
    user: Any,
    from_alias: str,
    to_alias: str,
    endpoint: str,
) -> str:
    """Log the hop and return the updated ``X-Model-Fallback`` reason."""
    logger.warning(
        "Rate limit fallback | backend={} user={} model={} -> {} endpoint={}",
        backend, getattr(user, "username", "?"), from_alias, to_alias, endpoint,
    )
    note = f"rate limited: {from_alias} (429) -> {to_alias}"
    return f"{fallback_reason}; {note}" if fallback_reason else note
