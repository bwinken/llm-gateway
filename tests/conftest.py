"""
Shared fixtures for LLM Gateway tests.

Every test uses an in-memory SQLite DB and a mocked httpx client so that
no real downstream servers are contacted.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

# Force SQLite for tests so psycopg2 is not required
os.environ["DATABASE_URL"] = "sqlite://"

import httpx
import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.schema import User

# ---------------------------------------------------------------------------
# Database — use StaticPool so all connections share the same in-memory DB
# ---------------------------------------------------------------------------

_test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def _reset_tables():
    SQLModel.metadata.drop_all(_test_engine)
    SQLModel.metadata.create_all(_test_engine)


# ---------------------------------------------------------------------------
# JWT test helpers — HS256 with a fixed secret for test convenience
# ---------------------------------------------------------------------------

_TEST_JWT_SECRET = "test-secret-for-jwt-signing"


def _make_test_jwt(
    sub: str = "testuser",
    scopes: list[str] | None = None,
    display_name: str | None = None,
    org_code: str | None = None,
) -> str:
    """Create a signed test JWT (HS256)."""
    payload: dict[str, Any] = {
        "sub": sub,
        "scopes": scopes or ["read"],
        "aud": "llm_gateway",
        "iss": "auth-center",
    }
    if display_name:
        payload["display_name"] = display_name
    if org_code:
        payload["org_code"] = org_code
    return pyjwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _test_decode_jwt(token: str) -> dict | None:
    """Decode a test JWT (HS256) — used to patch ``_decode_jwt`` in tests."""
    try:
        return pyjwt.decode(
            token,
            _TEST_JWT_SECRET,
            algorithms=["HS256"],
            audience="llm_gateway",
            issuer="auth-center",
            leeway=5,
        )
    except pyjwt.PyJWTError:
        return None


def web_auth_header(
    sub: str = "testuser",
    scopes: list[str] | None = None,
    display_name: str | None = None,
    org_code: str | None = None,
) -> dict[str, str]:
    """Return an Authorization header with a test JWT for web UI endpoints."""
    token = _make_test_jwt(sub=sub, scopes=scopes, display_name=display_name, org_code=org_code)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Helpers to build mock httpx responses
# ---------------------------------------------------------------------------

def make_httpx_response(
    status_code: int = 200,
    json_body: dict | None = None,
    text: str = "",
) -> httpx.Response:
    if json_body is not None:
        content = json.dumps(json_body).encode()
        headers = {"content-type": "application/json"}
    else:
        content = text.encode()
        headers = {"content-type": "text/plain"}
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers,
        request=httpx.Request("POST", "http://mock-downstream/v1/test"),
    )


class FakeStreamResponse:
    """Simulates an httpx streaming response with aiter_lines."""

    def __init__(self, lines: list[str], status_code: int = 200):
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        pass


# ---------------------------------------------------------------------------
# Test model routing / pricing
# ---------------------------------------------------------------------------

TEST_MODEL_ROUTING: dict[str, dict[str, Any]] = {
    "test-llm": {
        "base_url": "http://mock-llm:8000/v1",
        "real_model": "real-llm-v1",
        "api_key": "VLLM_API_KEY",
        "type": "llm",
    },
    "test-vlm": {
        "base_url": "http://mock-vlm:8001/v1",
        "real_model": "real-vlm-v1",
        "api_key": "VLM_API_KEY",
        "type": "vlm",
        # Optional metadata — exercises the /v1/models metadata pass-through
        "display_name": "Test VLM",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "supports_tools": True,
        "supports_vision": True,
    },
    "test-embedding": {
        "base_url": "http://mock-embed:8080/v1",
        "real_model": "BAAI/bge-m3",
        "api_key": "EMBEDDING_API_KEY",
        "type": "embedding",
    },
    "test-reranker": {
        "base_url": "http://mock-rerank:8080/v1",
        "real_model": "BAAI/bge-reranker-v2-m3",
        "api_key": "RERANK_API_KEY",
        "type": "reranker",
    },
    "test-vision-embedding": {
        "base_url": "http://mock-vembed:8090/v1",
        "real_model": "Qwen/Qwen3-VL-Embedding-2B",
        "api_key": "VEMBED_API_KEY",
        "type": "vision_embedding",
    },
    "test-vision-reranker": {
        "base_url": "http://mock-vrerank:8091/v1",
        "real_model": "Qwen/Qwen3-VL-Reranker-2B",
        "api_key": "VRERANK_API_KEY",
        "type": "vision_reranker",
    },
}

TEST_PRICING_MAP: dict[str, dict[str, float]] = {
    "_default": {"input_price_per_1m": 0.10, "output_price_per_1m": 0.10},
    "llm": {"input_price_per_1m": 0.50, "output_price_per_1m": 1.50},
    "vlm": {"input_price_per_1m": 5.00, "output_price_per_1m": 15.00},
    "embedding": {"input_price_per_1m": 0.02, "output_price_per_1m": 0.00},
    "reranker": {"input_price_per_1m": 0.05, "output_price_per_1m": 0.00},
    "vision_embedding": {"input_price_per_1m": 0.02, "output_price_per_1m": 0.00},
    "vision_reranker": {"input_price_per_1m": 0.05, "output_price_per_1m": 0.00},
}

TEST_FALLBACK_MAP: dict[str, str] = {}

# Azure OpenAI test models — separate map, parallel to MODEL_ROUTING.
TEST_AZURE_MODELS: dict[str, dict[str, Any]] = {
    "azure-gpt-4": {
        "type": "llm",
        "endpoint": "https://test.openai.azure.com",
        "deployment": "gpt-4-deploy",
        "api_key": "azure-test-key",
        "api_version": "2024-08-01-preview",
    },
    "azure-embed": {
        "type": "embedding",
        "endpoint": "https://test.openai.azure.com",
        "deployment": "embed-deploy",
        "api_key": "azure-test-key",
        "api_version": "2024-08-01-preview",
    },
}

# ---------------------------------------------------------------------------
# Build a test FastAPI app (no lifespan side-effects)
# ---------------------------------------------------------------------------

_mock_httpx_client = AsyncMock(spec=httpx.AsyncClient)


def _build_test_app() -> FastAPI:
    """Create a stripped-down app that skips lifespan (no health checks, no real DB init)."""

    @asynccontextmanager
    async def _noop_lifespan(app: FastAPI):
        yield

    # Patch config before importing routers
    with (
        patch("app.core.config.MODEL_ROUTING", TEST_MODEL_ROUTING),
        patch("app.core.config.PRICING_MAP", TEST_PRICING_MAP),
        patch("app.core.config.FALLBACK_MAP", TEST_FALLBACK_MAP),
        patch("app.core.config.AZURE_MODELS", TEST_AZURE_MODELS),
    ):
        from app.routers import admin, azure_api, vllm_api, web_ui

    test_app = FastAPI(lifespan=_noop_lifespan)

    # Register the same AccountDisabledError handler used in production so
    # tests against /dashboard etc. with disabled users see the same response.
    from app.main import account_disabled_handler
    from app.core.auth import AccountDisabledError
    test_app.add_exception_handler(AccountDisabledError, account_disabled_handler)

    test_app.include_router(web_ui.router)
    test_app.include_router(vllm_api.router)
    test_app.include_router(azure_api.router)
    test_app.include_router(admin.router)

    return test_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_all():
    """Apply all patches for every test."""
    import app.services.vllm_proxy  # noqa: F811 — ensure module is loaded before patching

    with (
        patch("app.services.vllm_proxy.MODEL_ROUTING", TEST_MODEL_ROUTING),
        patch("app.services.vllm_proxy.PRICING_MAP", TEST_PRICING_MAP),
        patch("app.services.vllm_proxy.FALLBACK_MAP", TEST_FALLBACK_MAP),
        patch("app.services.vllm_proxy.get_client", return_value=_mock_httpx_client),
        patch("app.services.vllm_proxy.engine", _test_engine),
        patch("app.services.vllm_proxy.is_alive", return_value=True),
        patch("app.services.azure_proxy.AZURE_MODELS", TEST_AZURE_MODELS),
        patch("app.services.azure_proxy.get_client", return_value=_mock_httpx_client),
        patch("app.core.config.AZURE_MODELS", TEST_AZURE_MODELS),
        patch("app.core.config.get_model_routing_snapshot", return_value=TEST_MODEL_ROUTING),
        patch("app.core.config.get_azure_models_snapshot", return_value=TEST_AZURE_MODELS),
        patch("app.core.server_state.get_client", return_value=_mock_httpx_client),
        patch("app.core.auth._decode_jwt", _test_decode_jwt),
    ):
        yield


@pytest.fixture()
def db_session():
    """Fresh DB session per test with clean tables."""
    _reset_tables()
    session = Session(_test_engine)
    yield session
    session.close()


@pytest.fixture()
def test_user(db_session: Session) -> User:
    user = User(
        username="testuser",
        password_hash="unused",
        api_key="sk-testkey123",
        daily_limit_usd=100.0,
        is_admin=False,
        is_disabled=False,
        can_use_azure=True,   # ← default ON for tests so /azure/v1/* paths exercise normally
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def admin_user(db_session: Session) -> User:
    user = User(
        username="admin",
        password_hash="unused",
        api_key="sk-adminkey456",
        daily_limit_usd=999.0,
        is_admin=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def client(db_session, test_user):
    """
    FastAPI TestClient with mocked deps.
    Access the httpx mock via client.__httpx_mock__.
    """
    from app.core.database import get_session

    app = _build_test_app()

    def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session

    # Reset the shared mock
    _mock_httpx_client.reset_mock()

    with TestClient(app, raise_server_exceptions=False) as tc:
        tc.__httpx_mock__ = _mock_httpx_client  # type: ignore[attr-defined]
        yield tc

    app.dependency_overrides.clear()


def auth_header(api_key: str = "sk-testkey123") -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}
