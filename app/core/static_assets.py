"""The web pages' own files under ``app/static`` and the URLs that load them.

Everything a page needs is served from this site: the built Tailwind
stylesheet (``css/tailwind.css``, made by ``scripts/build_css.sh``),
Chart.js and the two fonts (``vendor/``, ``fonts/``, fetched by
``scripts/vendor_frontend.sh``). Nothing loads from a CDN, so the pages work
on a network without internet access and no third-party script runs next to
the admin's API keys.

``static_url`` gives each file a URL that changes with its content
(``/static/css/tailwind.css?v=<hash>``): nginx can then let browsers keep a
file for a long time, and a new build is fetched at once because its URL is
new. nginx normally serves ``/static/`` straight from the checkout
(``deploy/llm-gateway-example.nginx.conf``); ``mount`` lets the gateway serve
it too, for a site whose nginx has no such location.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

STATIC_DIR: Path = Path(__file__).resolve().parent.parent / "static"
URL_PREFIX = "/static"


@lru_cache(maxsize=64)
def _digest(path: str, mtime_ns: int, size: int) -> str:
    # Keyed on mtime and size as well: a file replaced while the gateway runs
    # gets a new hash without a restart.
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]


def static_url(path: str) -> str:
    """``/static/<path>?v=<first 12 hex of its SHA-256>``. A missing file
    keeps a URL (``v=missing``) rather than failing the page."""
    file = STATIC_DIR / path
    try:
        stat = file.stat()
        version = _digest(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        version = "missing"
    return f"{URL_PREFIX}/{path}?v={version}"


def mount(app: FastAPI) -> None:
    """Serve ``app/static`` at ``/static`` (no sign-in: nothing in it is private)."""
    app.mount(URL_PREFIX, StaticFiles(directory=STATIC_DIR), name="static")
