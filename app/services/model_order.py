"""One display order for models, shared by every page that lists them.

The welcome page, the dashboard's model tables and its Cost by Model card
all sort with these keys, so a model sits in the same place everywhere:

1. by type, in ``TYPE_ORDER`` (unknown types after, by name);
2. models with an admin-set ``sort_order`` first, lowest number first;
3. then cheapest first (billed input + output price per 1M tokens);
4. then by alias in natural order: case-insensitive, digit runs compared as
   numbers, so ``qwen3-8b < qwen3-32b < qwen3-235b`` (a plain string sort
   put ``235b`` first and every capitalized alias ahead of the rest).

Models in a usage report that are no longer configured come after the
configured ones of their type, by alias.
"""

from __future__ import annotations

import math
import re
from typing import Any

TYPE_ORDER: tuple[str, ...] = (
    "llm", "vlm", "embedding", "vision_embedding", "reranker", "vision_reranker", "systemone",
)

_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Case-insensitive sort key that compares runs of digits as numbers."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in _DIGITS.split(str(text).casefold())
        if part
    )


def type_rank(model_type: str | None) -> tuple:
    """Position of a model type: ``TYPE_ORDER`` first, any other type after, by name."""
    t = model_type or ""
    return (TYPE_ORDER.index(t), "") if t in TYPE_ORDER else (len(TYPE_ORDER), t)


def sort_order_of(entry: dict[str, Any]) -> int | None:
    """The entry's ``sort_order`` when it is a usable integer, else None."""
    value = entry.get("sort_order")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def catalog_key(alias: str, sort_order: int | None, price_total: float) -> tuple:
    """Order of one model within its type (rules 2–4 in the module docstring)."""
    return (
        0 if sort_order is not None else 1,
        sort_order if sort_order is not None else 0,
        price_total,
        natural_key(alias),
    )


def unknown_key(alias: str) -> tuple:
    """Order of a model that is not in the config: after every configured one."""
    return (2, 0, math.inf, natural_key(alias))
