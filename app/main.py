"""
FastAPI application entry point.

Lifespan: DB init, global httpx client, background health checks.
Middleware: CORS.
Router mounting: health_api, web_ui, v1_api, azure_api, aws_api, admin.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.core.auth import AccountDisabledError
from app.core.config import APP_TITLE, get_site_links
from app.core.database import init_db
from app.core.logger import logger
from app.core.server_state import close_client, init_client
from app.routers import admin, aws_api, azure_api, health_api, v1_api, web_ui
from app.services.health import health_check_loop

_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
# disabled.html extends base.html, which renders the admin-editable site
# links (support bot / install guide) — same global as the router templates.
_templates.env.globals["get_site_links"] = get_site_links


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting {}...", APP_TITLE)
    init_db()
    await init_client()

    # Initialise Langfuse observability (no-op when LANGFUSE_* unset).
    from app.services.observability import get_langfuse
    get_langfuse()

    # Launch background health checker
    health_task = asyncio.create_task(health_check_loop(interval=30))
    logger.info("{} ready.", APP_TITLE)

    yield

    # Shutdown
    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass
    await close_client()
    from app.services.observability import flush_langfuse
    flush_langfuse()
    logger.info("{} shut down.", APP_TITLE)


app = FastAPI(
    title=APP_TITLE,
    version="1.0.0",
    description="High-performance API gateway for LLM/VLM/Embedding servers.",
    lifespan=lifespan,
)

# --- Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Stash request headers for the observability hook (read at the _log_usage
# seam). Pure-ASGI so the contextvar survives into streaming response bodies.
from app.services.observability import RequestMetaMiddleware  # noqa: E402
app.add_middleware(RequestMetaMiddleware)

# --- Exception handlers ---

@app.exception_handler(AccountDisabledError)
async def account_disabled_handler(request: Request, exc: AccountDisabledError):
    """Render an HTML page for browser requests, return JSON for API clients.

    Discriminates by the Accept header: if the client mentions text/html
    (browsers, dashboard) we render the styled disabled.html template;
    otherwise (curl, SDKs, AJAX with `Accept: application/json`) we return
    the plain JSON 403 the API contract expects.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept.lower():
        return _templates.TemplateResponse(
            "disabled.html",
            {
                "request": request,
                "app_title": APP_TITLE,
                "title": "Account Disabled",
                "username": exc.username,
                "user": None,
            },
            status_code=403,
        )
    return JSONResponse(
        status_code=403,
        content={"detail": "Account disabled. Contact your administrator."},
    )


# --- Routers ---
app.include_router(health_api.router)
app.include_router(web_ui.router)
app.include_router(v1_api.router)
app.include_router(azure_api.router)
app.include_router(aws_api.router)
app.include_router(admin.router)
