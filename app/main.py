"""
FastAPI application entry point.

Lifespan: DB init, global httpx client, background health checks.
Middleware: CORS.
Router mounting: web_ui, vllm_api, azure_api, admin.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import APP_TITLE
from app.core.database import init_db
from app.core.logger import logger
from app.core.server_state import close_client, init_client
from app.routers import admin, azure_api, vllm_api, web_ui
from app.services.health import health_check_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting {}...", APP_TITLE)
    init_db()
    await init_client()

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

# --- Routers ---
app.include_router(web_ui.router)
app.include_router(vllm_api.router)
app.include_router(azure_api.router)
app.include_router(admin.router)
