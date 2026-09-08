"""The API-key auth dependencies must not hold a pooled DB connection past
the auth step.

Background: ``get_current_user`` used to take ``Depends(get_session)``, a
``yield`` dependency. FastAPI (>= 0.118) runs a yield dependency's exit code
only after the response has been sent — streaming responses included — so
every in-flight ``/v1/chat/completions`` stream kept one PostgreSQL
connection checked out, idle in transaction, for its whole duration. With
enough concurrent Claude Code streams the QueuePool (20 + 15 overflow)
filled up and every new request died at auth with
``QueuePool limit of size 20 overflow 15 reached, connection timed out``.

These tests run the dependencies against a real ``QueuePool`` engine (the
shared test engine is a ``StaticPool``, whose checkout count is meaningless)
and assert the pool is empty as soon as the dependency returns, and stays
empty while a stream is being served.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import object_session
from sqlalchemy.pool import QueuePool
from sqlmodel import Session, SQLModel, create_engine

from app.core import deps
from app.models.schema import User
from tests.conftest import FakeStreamResponse, auth_header

_KEY = "sk-pooltest-abc"


@pytest.fixture()
def pooled_engine(tmp_path):
    """A file-backed SQLite engine on a QueuePool with exactly ONE slot, so a
    connection held past the auth step is visible as ``checkedout() == 1``
    (and a second checkout would time out, like production did)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(username="pooluser", password_hash="unused", api_key=_KEY, daily_limit_usd=10.0))
        s.commit()
    assert engine.pool.checkedout() == 0
    with patch("app.core.deps.engine", engine):
        yield engine
    engine.dispose()


def _bearer(key: str = _KEY) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)


class TestDependenciesReleaseConnection:
    def test_pool_detects_a_held_session(self, pooled_engine):
        """Sanity check that the probe can see the failure mode at all: a
        session kept open after its first query pins a pool slot."""
        with Session(pooled_engine) as s:
            s.exec(User.__table__.select()).first()
            assert pooled_engine.pool.checkedout() == 1
        assert pooled_engine.pool.checkedout() == 0

    def test_get_current_user_returns_with_pool_empty(self, pooled_engine):
        user = deps.get_current_user(credentials=_bearer(), x_api_key=None)

        assert user.username == "pooluser"
        assert pooled_engine.pool.checkedout() == 0
        # Detached, but fully loaded — downstream only reads columns.
        assert object_session(user) is None
        assert user.daily_limit_usd == 10.0
        assert user.is_admin is False

    def test_get_current_user_releases_on_rejection(self, pooled_engine):
        with pytest.raises(Exception):
            deps.get_current_user(credentials=_bearer("sk-wrong"), x_api_key=None)
        assert pooled_engine.pool.checkedout() == 0

    def test_cloud_access_dependencies_release_connection(self, pooled_engine):
        user = deps.get_current_user(credentials=_bearer(), x_api_key=None)
        user.can_use_azure = True
        user.can_use_bedrock = True
        user.azure_daily_limit_usd = 5.0
        user.bedrock_daily_limit_usd = 5.0

        assert deps.require_azure_access(user=user) is user
        assert pooled_engine.pool.checkedout() == 0
        assert deps.require_bedrock_access(user=user) is user
        assert pooled_engine.pool.checkedout() == 0


class TestStreamDoesNotHoldConnection:
    def test_chat_stream_serves_with_pool_empty(self, client, pooled_engine):
        """End-to-end through the app: while the SSE body is being produced
        the auth dependency's connection must already be back in the pool."""
        seen: list[int] = []

        class ProbingStream(FakeStreamResponse):
            async def aiter_lines(self):
                async for line in super().aiter_lines():
                    seen.append(pooled_engine.pool.checkedout())
                    yield line

        sse_lines = [
            'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}',
            'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
            "data: [DONE]",
        ]
        mock = client.__httpx_mock__
        mock.build_request.return_value = httpx.Request("POST", "http://mock-llm:8000/v1/chat/completions")
        mock.send.return_value = ProbingStream(sse_lines)

        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test-llm", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            headers=auth_header(_KEY),
        )

        assert resp.status_code == 200
        assert "Hi" in resp.text
        assert seen, "stream body was never produced"
        assert seen == [0] * len(seen), f"auth connection still checked out mid-stream: {seen}"
        assert pooled_engine.pool.checkedout() == 0
