"""
Admin panel: web UI + API endpoints for user management and model configuration.

One router, ``/admin``, with the admin scope required on every route
(``common.router``). The routes live in one module per tab of the page and
register themselves when imported, so the import below is what puts them on
the router.

This package exports ``router`` and nothing else. A helper is imported from
the module that defines it (``app.routers.admin.users._user_table``), and a
test patches a name in the module that looks it up
(``app.routers.admin.model_config.save_config``).
"""

from app.routers.admin.common import router
from app.routers.admin import (  # noqa: F401  (imported to register their routes)
    page,
    overview,
    users,
    clients,
    security,
    settings,
    model_config,
)

__all__ = ["router"]
