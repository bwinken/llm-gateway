"""Audit trail (app/services/audit.py, table audit_log).

Every unsafe request under /admin and /dashboard, every full API key shown
and every export is recorded by middleware, with actor, route, status,
client, target account and the submitted fields; secrets are redacted.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from sqlmodel import select

from app.models.schema import AuditLog, User
from tests.conftest import auth_header, web_auth_header

JSON = {"Accept": "application/json"}


def _hdr(admin: User) -> dict:
    return web_auth_header(sub=admin.username, scopes=["admin"])


def _entries(db_session) -> list[AuditLog]:
    db_session.expire_all()
    return list(db_session.exec(select(AuditLog).order_by(AuditLog.id)).all())


def _detail(entry: AuditLog) -> dict:
    return json.loads(entry.detail)


class TestWhatIsRecorded:
    def test_an_admin_change(self, client, db_session, admin_user, test_user):
        resp = client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "25"},
                           headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 200
        [e] = _entries(db_session)
        assert (e.actor, e.method, e.route, e.status) == (
            "admin", "POST", "/admin/users/{user_id}/limit", 200)
        assert e.path == f"/admin/users/{test_user.id}/limit"
        assert e.client == "testclient"
        d = _detail(e)
        assert d["target"] == "testuser"
        assert d["request"] == {"new_limit": "25"}
        assert d["response"]["ok"] is True

    def test_a_deleted_account_is_still_named(self, client, db_session, admin_user):
        victim = User(username="victim")
        db_session.add(victim)
        db_session.commit()
        client.post(f"/admin/users/{victim.id}/delete", headers={**_hdr(admin_user), **JSON})
        [e] = _entries(db_session)
        assert _detail(e)["target"] == "victim"

    def test_refused_and_failed_attempts_are_recorded(self, client, db_session, admin_user, test_user):
        # Validation error, then a non-admin, then a cross-site attempt.
        client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "-1"},
                    headers={**_hdr(admin_user), **JSON})
        client.post(f"/admin/users/{admin_user.id}/toggle-disable",
                    headers={**web_auth_header(sub=test_user.username, scopes=["read"]), **JSON})
        client.post(f"/admin/users/{test_user.id}/toggle-disable",
                    headers={**_hdr(admin_user), "Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"})
        statuses = [(e.actor, e.status) for e in _entries(db_session)]
        assert statuses == [("admin", 400), ("testuser", 403), ("admin", 403)]

    def test_a_user_regenerating_their_key(self, client, db_session, test_user):
        resp = client.post("/dashboard/refresh-key", headers=web_auth_header(sub=test_user.username))
        new_key = resp.json()["api_key"]
        [e] = _entries(db_session)
        assert (e.actor, e.route) == ("testuser", "/dashboard/refresh-key")
        assert _detail(e)["response"]["api_key"] == "<redacted>"
        assert new_key not in e.detail

    def test_revealing_a_key_is_recorded_without_the_key(self, client, db_session, admin_user, test_user):
        resp = client.get(f"/admin/users/{test_user.id}/api-key", headers=_hdr(admin_user))
        assert resp.status_code == 200
        [e] = _entries(db_session)
        assert e.route == "/admin/users/{user_id}/api-key"
        assert _detail(e)["target"] == "testuser"
        assert "sk-testkey123" not in e.detail

    def test_exports_are_recorded(self, client, db_session, admin_user):
        client.get("/admin/api/export/users.csv", headers=_hdr(admin_user))
        [e] = _entries(db_session)
        assert e.route == "/admin/api/export/users.csv"

    def test_config_save_records_a_diff_not_the_config(self, client, db_session, admin_user):
        body = client.get("/admin/api/config", headers=_hdr(admin_user)).json()
        body["azure_models"]["azure-gpt-4"]["deployment"] = "new-deploy"
        body["azure_models"]["azure-gpt-4"]["api_key"] = "brand-new-secret"
        with patch("app.routers.admin.save_config"):
            assert client.put("/admin/api/config", json=body, headers=_hdr(admin_user)).status_code == 200
        [e] = _entries(db_session)
        d = _detail(e)
        assert "request" not in d
        changed = d["config_changes"]["azure_models"]["changed"]["azure-gpt-4"]
        assert {"deployment", "api_key"} <= set(changed)
        assert "brand-new-secret" not in e.detail


class TestWhatIsNot:
    def test_reads_and_probes(self, client, db_session, admin_user):
        client.get("/admin", headers=_hdr(admin_user))
        client.get("/admin/user-table", params={"kind": "users"}, headers=_hdr(admin_user))
        client.post("/admin/api/test-model", json={"base_url": "nope"}, headers=_hdr(admin_user))
        assert _entries(db_session) == []

    def test_api_traffic(self, client, db_session):
        client.get("/v1/me", headers=auth_header())
        client.post("/v1/tokenize", json={"model": "test-llm", "prompt": "x"}, headers=auth_header())
        assert _entries(db_session) == []


class TestRobustness:
    def test_a_failing_write_never_fails_the_request(self, client, admin_user, test_user):
        with patch("app.services.audit.Session", side_effect=RuntimeError("db down")):
            resp = client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "5"},
                               headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 200

    def test_the_record_survives_in_the_log_when_the_write_fails(self, admin_user):
        from app.services import audit

        with patch("app.services.audit.Session", side_effect=RuntimeError("db down")), \
             patch("app.services.audit.logger") as log:
            audit._write({"actor": "admin", "method": "POST", "route": "/admin/x", "path": "/admin/x",
                          "status": 200, "client": "", "detail": {"k": 1}})
        args = log.error.call_args.args
        assert "Audit write failed" in args[0]
        assert '"route": "/admin/x"' in args[-1]

    def test_redaction(self):
        from app.services.audit import redact

        out = redact({"api_key": "s", "aws_secret_access_key": "s", "Authorization": "Bearer x",
                      "password": "p", "nested": {"token": "t", "ok": "v"}, "empty_key": "", "long": "x" * 500})
        assert out["api_key"] == out["aws_secret_access_key"] == out["Authorization"] == "<redacted>"
        assert out["password"] == "<redacted>"
        assert out["nested"] == {"token": "<redacted>", "ok": "v"}
        assert out["long"].startswith("x" * 200) and out["long"].endswith("<500>")


class TestAdminView:
    def test_settings_tab_lists_recent_entries(self, client, admin_user, test_user):
        client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "7"},
                    headers={**_hdr(admin_user), **JSON})
        page = client.get("/admin", headers=_hdr(admin_user)).text
        assert 'id="auditLogCard"' in page
        assert "/admin/users/{user_id}/limit" in page
        assert "target=testuser" in page

    def test_json_api_pages_and_filters(self, client, admin_user, test_user):
        for v in ("1", "2", "3"):
            client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": v},
                        headers={**_hdr(admin_user), **JSON})
        first = client.get("/admin/api/audit-log", params={"limit": 2}, headers=_hdr(admin_user)).json()["entries"]
        assert [e["detail"]["request"]["new_limit"] for e in first] == ["3", "2"]
        rest = client.get("/admin/api/audit-log", params={"before_id": first[-1]["id"]},
                          headers=_hdr(admin_user)).json()["entries"]
        assert [e["detail"]["request"]["new_limit"] for e in rest] == ["1"]
        assert client.get("/admin/api/audit-log", params={"actor": "nobody"},
                          headers=_hdr(admin_user)).json()["entries"] == []

    def test_csv_export(self, client, admin_user, test_user):
        client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "9"},
                    headers={**_hdr(admin_user), **JSON})
        resp = client.get("/admin/api/export/audit-log.csv", headers=_hdr(admin_user))
        assert resp.status_code == 200
        assert "/admin/users/{user_id}/limit" in resp.text

    def test_only_admins(self, client, test_user):
        resp = client.get("/admin/api/audit-log", headers=web_auth_header(sub=test_user.username, scopes=["read"]))
        assert resp.status_code == 403
