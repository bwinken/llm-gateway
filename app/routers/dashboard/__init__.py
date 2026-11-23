"""
The user dashboard: what a signed-in person sees of their own account.

One router (``common.router``), SSO on every route, everything under
``/dashboard``. Three modules, split by what a frontend of its own would
keep:

``page``     the page the gateway renders itself today (``GET /dashboard``
             and the two panels it swaps in). This is the part a separate
             frontend replaces.
``api``      the same data as JSON (``/dashboard/api/*``): what that
             frontend, or anything else signed in by SSO, reads.
``actions``  what the account does and downloads: regenerate a key, report
             a notice as applied, the patch scripts. They answer JSON to a
             caller that asks for it, so they serve the page and a frontend
             alike.

What any of them answers with is put together in
``app/services/dashboard_view.py``; the modules here authenticate and
answer. They register their routes on ``router`` when imported, so the
import below is what puts them there. The package exports ``router`` and
nothing else: import a helper from the module that defines it, and patch a
name in the module that looks it up.
"""

from app.routers.dashboard.common import router
from app.routers.dashboard import (  # noqa: F401  (imported to register their routes)
    page,
    api,
    actions,
)

__all__ = ["router"]
