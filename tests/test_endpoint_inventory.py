"""``docs/endpoints.md`` lists every route the gateway registers, with who
calls it and whether it stays. It is only worth reading while it is true:
a route added or removed without its line there fails here."""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "endpoints.md"
# A route's line: | `GET /v1/me` | `v1_api.account_info` |
ROW = re.compile(r"^\| `(GET|POST|PUT|PATCH|DELETE) (/[^`]*)` \| `([\w.]+)` \|$", re.M)


def _registered() -> dict[tuple[str, str], str]:
    from fastapi.routing import iter_route_contexts

    from app.main import app

    routes = {}
    for context in iter_route_contexts(app.routes):
        if not context.methods:
            continue  # the /static mount: files, not a route
        endpoint = context.route.endpoint
        handler = f"{endpoint.__module__.replace('app.routers.', '')}.{endpoint.__name__}"
        for method in context.methods:
            if method != "HEAD":
                routes[(method, context.path)] = handler
    return routes


def _listed() -> dict[tuple[str, str], str]:
    return {(method, path): handler for method, path, handler in ROW.findall(DOC.read_text(encoding="utf-8"))}


def test_the_inventory_lists_exactly_the_routes_there_are():
    registered, listed = _registered(), _listed()
    assert len(registered) > 100  # the walk does find the routes
    assert sorted(set(registered) - set(listed)) == [], "registered but not in docs/endpoints.md"
    assert sorted(set(listed) - set(registered)) == [], "in docs/endpoints.md but no longer registered"
    assert {key: listed[key] for key in registered} == registered  # each under the handler that serves it


def test_each_route_is_listed_once_and_the_total_is_right():
    text = DOC.read_text(encoding="utf-8")
    rows = ROW.findall(text)
    assert len(rows) == len({(method, path) for method, path, _ in rows})
    assert f"共 **{len(rows)}** 條" in text


def test_what_was_removed_stays_removed():
    registered = {path for _, path in _registered()}
    assert "/redoc" not in registered and "/docs/oauth2-redirect" not in registered
