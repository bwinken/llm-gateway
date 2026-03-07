"""
FastAPI application entry point.

Lifespan: DB init, global httpx client, background health checks.
Middleware: SessionMiddleware, CORS.
Router mounting: web_ui, auth_api, llm_api, admin.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import SECRET_KEY
from app.core.database import init_db
from app.core.logger import logger
from app.core.server_state import close_client, init_client
from app.routers import admin, auth_api, llm_api, web_ui
from app.services.health import health_check_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting LLM Gateway...")
    init_db()
    await init_client()

    # Launch background health checker
    health_task = asyncio.create_task(health_check_loop(interval=30))
    logger.info("LLM Gateway ready.")

    yield

    # Shutdown
    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass
    await close_client()
    logger.info("LLM Gateway shut down.")


app = FastAPI(
    title="LLM Gateway",
    version="1.0.0",
    description="High-performance API gateway for LLM/VLM/Embedding servers.",
    lifespan=lifespan,
)

# --- Middleware ---
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routers ---
app.include_router(web_ui.router)
app.include_router(auth_api.router)
app.include_router(llm_api.router)
app.include_router(admin.router)
