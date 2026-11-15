"""Which on-prem model serves a request whose own model cannot.

The one rule, shared by the request path (``vllm_proxy._resolve_model``) and
the pages that say what happens when a server is down (the dashboard's model
list, ``GET /v1/status``), so the two can never disagree:

1. the configured fallback of an allowed type (``[fallback]`` in config.toml),
   if it is up;
2. else the first model of an allowed type that is up, in config order;
3. else, best effort, the first model of an allowed type, up or not.

Pure: the caller passes the routing table, the fallback map and the liveness
check, so each caller keeps reading its own (patched in tests) names, and
nothing here logs.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

How = Literal["configured", "auto", "best_effort"]


def pick(
    model_name: str,
    allowed_types: list[str],
    routing: dict[str, dict[str, Any]],
    fallback_map: dict[str, str],
    alive: Callable[[str], bool],
) -> tuple[str, dict[str, Any], How] | None:
    """``(alias, route, how)`` of the model that takes over, or None when
    no model of an allowed type is configured at all."""
    for model_type in allowed_types:
        alias = fallback_map.get(model_type)
        if alias and alias != model_name and alias in routing:
            route = routing[alias]
            if alive(route["base_url"]):
                return alias, route, "configured"
    snapshot = dict(routing)
    for alias, route in snapshot.items():
        if route["type"] in allowed_types and alive(route["base_url"]):
            return alias, route, "auto"
    for alias, route in snapshot.items():
        if route["type"] in allowed_types:
            return alias, route, "best_effort"
    return None


def allowed_types_for(model_type: str) -> list[str]:
    """The types a request for a model of ``model_type`` may fall back to:
    what the endpoints that serve that type accept (``v1_api``: chat and
    messages take ``llm`` / ``vlm``, embeddings both embedding types,
    rerank both reranker types)."""
    for group in (["llm", "vlm"], ["embedding", "vision_embedding"], ["reranker", "vision_reranker"]):
        if model_type in group:
            # The model's own type first: the endpoint tries the types in
            # its order, but a same-type stand-in is what the page should
            # promise when one exists.
            return [model_type] + [t for t in group if t != model_type]
    return [model_type]
