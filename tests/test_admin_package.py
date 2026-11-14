"""``app/routers/admin/`` is one router whose routes live in one module per tab.

Two things about that layout fail silently, so they are pinned here: a module
the package does not import has no routes, and a name re-exported from the
package would turn a test's stale ``patch("app.routers.admin.<name>")`` into a
no-op (for ``save_config``, a real write of ``config.toml``).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from fastapi.routing import APIRoute

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "app" / "routers" / "admin"


def _modules() -> dict[str, ModuleType]:
    from app.routers import admin

    return {name: value for name, value in vars(admin).items() if isinstance(value, ModuleType)}


def test_the_package_exports_the_router_and_nothing_else():
    from app.routers import admin

    exported = {
        name for name, value in vars(admin).items()
        if not name.startswith("__") and not isinstance(value, ModuleType)
    }
    assert exported == {"router"}


def test_every_module_in_the_package_is_imported_by_it():
    on_disk = {path.stem for path in PACKAGE_DIR.glob("*.py")} - {"__init__"}
    assert len(on_disk) >= 8
    assert on_disk == set(_modules())


def test_there_is_one_router():
    from app.routers import admin

    modules = _modules()
    for name, module in modules.items():
        if hasattr(module, "router"):
            assert module.router is admin.router, name
    # ... and every route on it comes from this package, each module but
    # `common` (which only holds what the others share) having some.
    owners = {route.endpoint.__module__ for route in admin.router.routes if isinstance(route, APIRoute)}
    assert owners == {f"app.routers.admin.{name}" for name in modules if name != "common"}


def test_a_fixed_path_is_matched_before_the_parameter_of_the_same_shape(client, admin_user):
    """``/client-rules/mail-all`` and ``/client-rules/{rule_id}`` are both
    two segments: registered the other way round, *Mail everyone* would be
    taken for a rule called ``mail-all`` (a 404)."""
    from app.routers import admin

    posts = [
        route.path for route in admin.router.routes
        if isinstance(route, APIRoute) and "POST" in route.methods and route.path.startswith("/admin/client-rules/")
    ]
    by_id = posts.index("/admin/client-rules/{rule_id}")
    for fixed in ("monthly-mail", "mail-all", "mail-test-all"):
        assert posts.index(f"/admin/client-rules/{fixed}") < by_id
