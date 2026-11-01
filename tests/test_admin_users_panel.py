"""The admin Users tab: the Users / Apps tables, their fragment endpoint
(GET /admin/user-table) and the in-place (JSON) per-account actions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.schema import AppOwner, UsageLog, User
from tests.conftest import web_auth_header

JSON = {"Accept": "application/json"}


def _hdr(admin: User) -> dict:
    return web_auth_header(sub=admin.username, scopes=["admin"])


def _user(session, username: str, **fields) -> User:
    u = User(username=username, **fields)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _spend(session, user: User, usd: float) -> None:
    session.add(UsageLog(
        user_id=user.id, model="test-llm", model_type="llm", input_tokens=1500,
        output_tokens=500, cost_usd=Decimal(str(usd)), endpoint="/v1/messages",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    session.commit()


def _panel(client, admin: User, **params) -> str:
    resp = client.get("/admin/user-table", params=params, headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    return resp.text


def _row_order(body: str, names: list[str]) -> list[str]:
    found = [(body.index(f'data-username="{n}"'), n) for n in names if f'data-username="{n}"' in body]
    return [n for _, n in sorted(found)]


class TestFragmentEndpoint:
    def test_requires_admin(self, client, test_user):
        resp = client.get("/admin/user-table", params={"kind": "users"},
                          headers=web_auth_header(sub=test_user.username, scopes=["read"]))
        assert resp.status_code == 403

    def test_rejects_unknown_kind(self, client, admin_user):
        resp = client.get("/admin/user-table", params={"kind": "everyone"}, headers=_hdr(admin_user))
        assert resp.status_code == 422

    def test_renders_one_panel(self, client, db_session, admin_user, test_user):
        _user(db_session, "app_bot")
        body = _panel(client, admin_user, kind="users")
        assert 'data-um-panel="users"' in body and 'data-um-panel="apps"' not in body
        assert "<html" not in body
        assert 'data-username="testuser"' in body and "app_bot" not in body

        body = _panel(client, admin_user, kind="apps")
        assert 'data-um-panel="apps"' in body and 'data-username="app_bot"' in body
        assert 'data-username="testuser"' not in body

    def test_month_column_is_this_month(self, client, db_session, admin_user, test_user):
        """The column used to say "All-time cumulative cost" over the
        current month's figure."""
        _spend(db_session, test_user, 2.5)
        body = _panel(client, admin_user, kind="users")
        assert "This Month" in body and "Total Spend" not in body and "All-time" not in body
        row = body[body.index('data-username="testuser"'):]
        assert "$2.50" in row[:row.index("</tr>")]
        assert "2.0K tokens · 1 reqs" in row[:row.index("</tr>")]

    def test_page_usage_matches_the_full_page(self, client, db_session, admin_user, test_user):
        """The fragment aggregates only its page's accounts; the figure must
        equal what the full page (month-wide map) shows."""
        _spend(db_session, test_user, 1.25)
        other = _user(db_session, "someone")
        _spend(db_session, other, 9)
        fragment = _panel(client, admin_user, kind="users", q="testuser")
        full = client.get("/admin", params={"q": "testuser"}, headers=_hdr(admin_user)).text
        def row(body: str) -> str:
            start = body.index('data-username="testuser"')
            return body[start:body.index("</tr>", start)]

        assert "$1.25" in row(fragment)
        assert row(fragment) == row(full)
        assert "$9.00" not in fragment


class TestSearchAndPaging:
    def test_paging_links_are_url_encoded(self, client, db_session, admin_user):
        """q went into the links raw: '&' or '#' in a search broke paging."""
        for i in range(17):
            _user(db_session, f"r&d#{i:02d}")
        body = _panel(client, admin_user, kind="users", q="r&d#")
        assert "1-15 of 17" in body
        assert "q=r%26d%23" in body
        assert "offset=15" in body
        assert 'data-um-nav="users"' in body

        body = _panel(client, admin_user, kind="users", q="r&d#", offset=15)
        assert "16-17 of 17" in body

    def test_links_keep_the_other_panels_offset(self, client, db_session, admin_user):
        for i in range(16):
            _user(db_session, f"u{i:02d}")
        body = _panel(client, admin_user, kind="users", app_offset=30)
        assert "app_offset=30" in body and "offset=15" in body

    def test_search_matches_display_name_and_org(self, client, db_session, admin_user):
        _user(db_session, "e1001", display_name="Alice Chen", org_code="RD-7")
        assert 'data-username="e1001"' in _panel(client, admin_user, kind="users", q="alice")
        assert 'data-username="e1001"' in _panel(client, admin_user, kind="users", q="rd-7")

    def test_empty_result_says_so(self, client, admin_user):
        body = _panel(client, admin_user, kind="users", q="nobody-matches-this")
        assert "No users match this search or filter." in body

    @pytest.mark.parametrize("limit, shown", [(0, "1-1 of"), (100000, "1-")])
    def test_page_size_is_bounded(self, client, db_session, admin_user, limit, shown):
        body = _panel(client, admin_user, kind="users", limit=limit)
        assert shown in body


class TestFiltersAndSort:
    def test_status_filters(self, client, db_session, admin_user, test_user):
        _user(db_session, "gone", is_disabled=True)
        _user(db_session, "clouder", can_use_bedrock=True)
        _user(db_session, "busy", concurrency_waived=True)
        names = ["testuser", "gone", "clouder", "busy"]

        assert _row_order(_panel(client, admin_user, kind="users", status="disabled"), names) == ["gone"]
        # test_user has Azure in the fixture.
        assert set(_row_order(_panel(client, admin_user, kind="users", status="cloud"), names)) == {"testuser", "clouder"}
        assert _row_order(_panel(client, admin_user, kind="users", status="waived"), names) == ["busy"]
        assert len(_row_order(_panel(client, admin_user, kind="users", status="bogus"), names)) == 4

    def test_sort_by_spend(self, client, db_session, admin_user, test_user):
        low, high = _user(db_session, "low"), _user(db_session, "high")
        _spend(db_session, low, 1)
        _spend(db_session, high, 5)
        body = _panel(client, admin_user, kind="users", sort="spend")
        assert _row_order(body, ["low", "high", "testuser"]) == ["high", "low", "testuser"]

    def test_sort_by_spend_pages(self, client, db_session, admin_user):
        for i in range(4):
            _spend(db_session, _user(db_session, f"s{i}"), i + 1)
        body = _panel(client, admin_user, kind="users", sort="spend", limit=2, offset=2)
        assert _row_order(body, ["s0", "s1", "s2", "s3"]) == ["s1", "s0"]

    def test_sort_by_name(self, client, db_session, admin_user):
        _user(db_session, "zed")
        _user(db_session, "amy")
        body = _panel(client, admin_user, kind="users", sort="name")
        assert _row_order(body, ["zed", "amy"]) == ["amy", "zed"]

    def test_full_page_keeps_the_state(self, client, db_session, admin_user):
        body = client.get("/admin", params={"q": "x", "sort": "spend", "status": "cloud"},
                          headers=_hdr(admin_user)).text
        assert '<option value="spend" selected' in body
        assert '<option value="cloud" selected' in body
        assert 'name="q" value="x"' in body


class TestRows:
    def test_disabled_row_is_marked(self, client, db_session, admin_user):
        _user(db_session, "gone", is_disabled=True)
        body = _panel(client, admin_user, kind="users")
        row = body[body.rindex("<tr", 0, body.index('data-username="gone"')):]
        row = row[:row.index("</tr>")]
        assert "is-disabled" in row
        assert 'um-disabled-badge "' in row  # badge shown (no "hidden")

    def test_no_disable_or_delete_on_self_or_admins(self, client, db_session, admin_user, test_user):
        body = _panel(client, admin_user, kind="users")
        assert f"/admin/users/{admin_user.id}/delete" not in body
        assert f"/admin/users/{admin_user.id}/toggle-disable" not in body
        assert f"/admin/users/{test_user.id}/delete" in body

    def test_apps_list_only_their_own_owners(self, client, db_session, admin_user, test_user):
        bot = _user(db_session, "app_bot")
        other_owner = _user(db_session, "carol")
        _user(db_session, "app_other")
        db_session.add(AppOwner(app_id=bot.id, owner_id=test_user.id))
        db_session.add(AppOwner(app_id=bot.id, owner_id=other_owner.id))
        db_session.commit()
        body = _panel(client, admin_user, kind="apps")
        assert 'value="carol, testuser"' in body
        assert body.count('name="owner_usernames" value=""') == 1  # app_other

    def test_controls_are_in_place_actions(self, client, db_session, admin_user, test_user):
        body = _panel(client, admin_user, kind="users")
        for action in ("azure", "bedrock", "waive", "disable", "delete", "limit", "cloud-limit"):
            assert f'data-action="{action}"' in body
        assert "onsubmit" not in body and "confirm(" not in body


class TestJsonReplies:
    """The Users tab saves in place; the same endpoints still redirect for a
    plain form post."""

    def test_limit(self, client, db_session, admin_user, test_user):
        resp = client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "12.5"},
                           headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "daily_limit_usd": 12.5}

    def test_limit_rejects_negative_with_detail(self, client, admin_user, test_user):
        resp = client.post(f"/admin/users/{test_user.id}/limit", data={"new_limit": "-1"},
                           headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 400
        assert "new_limit" in resp.json()["detail"]

    def test_cloud_limit_set_and_clear(self, client, admin_user, test_user):
        url = f"/admin/users/{test_user.id}/cloud-limit"
        resp = client.post(url, data={"new_cloud_limit": "3"}, headers={**_hdr(admin_user), **JSON})
        assert resp.json() == {"ok": True, "cloud_daily_limit_usd": 3.0}
        resp = client.post(url, data={"new_cloud_limit": ""}, headers={**_hdr(admin_user), **JSON})
        assert resp.json() == {"ok": True, "cloud_daily_limit_usd": None}

    def test_toggle_disable(self, client, admin_user, test_user):
        url = f"/admin/users/{test_user.id}/toggle-disable"
        assert client.post(url, headers={**_hdr(admin_user), **JSON}).json() == {"ok": True, "is_disabled": True}
        assert client.post(url, headers={**_hdr(admin_user), **JSON}).json() == {"ok": True, "is_disabled": False}

    def test_toggle_disable_self_is_a_400_with_detail(self, client, admin_user):
        resp = client.post(f"/admin/users/{admin_user.id}/toggle-disable", headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Cannot disable yourself."

    def test_delete(self, client, db_session, admin_user):
        victim = _user(db_session, "victim")
        resp = client.post(f"/admin/users/{victim.id}/delete", headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "deleted": victim.id}
        db_session.expire_all()
        assert db_session.get(User, victim.id) is None

    @pytest.mark.parametrize("path, data", [
        ("limit", {"new_limit": "5"}),
        ("cloud-limit", {"new_cloud_limit": "5"}),
        ("toggle-disable", None),
    ])
    def test_plain_posts_still_redirect(self, client, admin_user, test_user, path, data):
        resp = client.post(f"/admin/users/{test_user.id}/{path}", data=data,
                           headers=_hdr(admin_user), follow_redirects=False)
        assert resp.status_code == 303


class TestAdminPage:
    def test_month_label_and_export_defaults_are_local(self, client, admin_user):
        """Report months are Asia/Taipei months. The label came from a UTC
        clock (the previous month for the first 8 hours of the 1st), and the
        export picker from toISOString, which put "From" a month early east
        of UTC."""
        with patch("app.routers.admin.local_month_start", return_value=date(2026, 3, 1)):
            body = client.get("/admin", headers=_hdr(admin_user)).text
        assert "March 2026" in body
        assert 'value="2025-10" max="2026-03"' in body  # From: 5 months back
        assert 'value="2026-03" max="2026-03"' in body  # To: this month
        assert "toISOString" not in body

    def test_leaderboards_say_top_10_and_have_no_pager(self, client, admin_user):
        body = client.get("/admin", headers=_hdr(admin_user)).text
        assert "· top 10" in body
        assert "lbPage" not in body

    def test_both_panels_and_counts_render(self, client, db_session, admin_user, test_user):
        _user(db_session, "app_bot")
        body = client.get("/admin", headers=_hdr(admin_user)).text
        assert 'data-um-panel="users"' in body and 'data-um-panel="apps"' in body
        assert 'data-um-count="apps">1<' in body
