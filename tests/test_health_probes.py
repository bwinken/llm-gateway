"""Tests for the unauthenticated /healthz and /readyz probes."""

from __future__ import annotations

from unittest.mock import patch


class TestHealthz:
    def test_ok_without_auth(self, client):
        """Liveness answers 200 with no API key and no SSO session."""
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ignores_bogus_credentials(self, client):
        """A garbage Bearer token must not turn a probe into a 401."""
        resp = client.get("/healthz", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 200


class TestReadyz:
    def test_ok_when_db_answers(self, client):
        resp = client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert set(body["downstreams"]) == {"alive", "total"}

    def test_503_when_db_unreachable(self, client):
        with patch("app.routers.health_api._ping_db", side_effect=OSError("no db")):
            resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["database"] == "error"

    def test_reports_downstream_health_counts(self, client):
        """Downstream counts are informational — a dead fleet is still ready."""
        with patch(
            "app.routers.health_api.all_health",
            return_value={"http://a:8000/v1": False, "http://b:8000/v1": False},
        ):
            resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["downstreams"] == {"alive": 0, "total": 2}
