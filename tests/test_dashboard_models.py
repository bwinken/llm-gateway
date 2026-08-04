"""
Dashboard model-list rendering: table layout + per-backend sections gated
by the user's cloud-access flags. A user without can_use_azure /
can_use_bedrock must not see those sections at all.
"""

from __future__ import annotations

from app.models.schema import User
from tests.conftest import web_auth_header


def _get_dashboard(client, username):
    return client.get(
        "/dashboard",
        headers=web_auth_header(sub=username, scopes=["read"]),
    )


class TestDashboardModelSections:

    def test_plain_user_sees_only_onprem(self, client, db_session):
        db_session.add(User(username="plainuser"))
        db_session.commit()

        resp = _get_dashboard(client, "plainuser")
        assert resp.status_code == 200
        body = resp.text
        assert "On-Prem Models" in body
        assert "gw-table" in body  # list layout, not cards
        assert "Azure OpenAI Models" not in body
        assert "AWS Bedrock Models" not in body

    def test_azure_user_sees_azure_section_only(self, client, db_session):
        db_session.add(User(username="azuser", can_use_azure=True))
        db_session.commit()

        resp = _get_dashboard(client, "azuser")
        assert resp.status_code == 200
        body = resp.text
        assert "Azure OpenAI Models" in body
        assert "/azure/v1" in body
        assert "AWS Bedrock Models" not in body

    def test_bedrock_user_sees_bedrock_section_only(self, client, db_session):
        db_session.add(User(username="bruser", can_use_bedrock=True))
        db_session.commit()

        resp = _get_dashboard(client, "bruser")
        assert resp.status_code == 200
        body = resp.text
        assert "AWS Bedrock Models" in body
        assert "/aws/v1" in body
        assert "Azure OpenAI Models" not in body

    def test_admin_sees_all_sections(self, client, db_session, admin_user):
        # Admin status is synced from JWT scopes on every request
        # (auth.py get_web_user), so the token must carry the admin scope —
        # a read-only token would demote the DB flag and hide the sections.
        resp = client.get(
            "/dashboard",
            headers=web_auth_header(sub=admin_user.username, scopes=["read", "admin"]),
        )
        assert resp.status_code == 200
        body = resp.text
        assert "On-Prem Models" in body
        assert "Azure OpenAI Models" in body
        assert "AWS Bedrock Models" in body

    def test_onprem_table_lists_models_with_status(self, client, db_session):
        db_session.add(User(username="tableuser"))
        db_session.commit()

        resp = _get_dashboard(client, "tableuser")
        assert resp.status_code == 200
        body = resp.text
        # Aliases from TEST_MODEL_ROUTING rendered as table rows
        assert "test-llm" in body
        assert "test-embedding" in body
        # Health column present (mocked is_alive returns True in tests)
        assert "ONLINE" in body or "DOWN" in body
