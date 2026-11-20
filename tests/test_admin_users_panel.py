"""The admin Users tab: the Users / Apps tables, their fragment endpoint
(GET /admin/user-table) and the in-place (JSON) per-account actions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.schema import AppOwner, AuditLog, UsageLog, User
from tests.conftest import earlier_today, web_auth_header

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
        created_at=earlier_today(),   # not a plain "5 minutes ago": that is last month just after midnight on the 1st
    ))
    session.commit()


def _panel(client, admin: User, **params) -> str:
    resp = client.get("/admin/user-table", params=params, headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    return resp.text


def _row(body: str, username: str) -> str:
    start = body.rindex("<tr", 0, body.index(f'data-username="{username}"'))
    return body[start:body.index("</tr>", start)]


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
        _user(db_session, "app_bot", is_service_account=True)
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
        assert "2.0K tok · 1 reqs" in row[:row.index("</tr>")]

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

    def test_a_row_shows_and_changes_nothing(self, client, db_session, admin_user, test_user):
        """The controls are in the account drawer; a row has the box that
        selects it and the two ways into the drawer."""
        body = _panel(client, admin_user, kind="users")
        assert "<form" not in body and "data-action" not in body and "/toggle-" not in body
        assert "/delete" not in body and "showKeyModal" not in body
        row = _row(body, "testuser")
        assert row.count(f'data-um-detail="{test_user.id}"') == 2        # the name and Manage
        assert f'data-um-select value="{test_user.id}"' in row
        assert "data-um-select-all" in body
        assert "onsubmit" not in body and "confirm(" not in body

    def test_a_row_says_what_differs_from_the_defaults(self, client, db_session, admin_user, test_user):
        _user(db_session, "plain")
        _user(db_session, "special", can_use_azure=True, concurrency_waived=True, can_overflow_cloud=False,
              cloud_daily_limit_usd=7.5, daily_limit_usd=25)
        body = _panel(client, admin_user, kind="users")
        plain, special = _row(body, "plain"), _row(body, "special")
        assert "um-chip" not in plain and ">defaults<" in plain
        for chip in ("um-chip-azure", "um-chip-waive", "um-chip-overflow"):
            assert chip in special, chip
        assert "um-chip-bedrock" not in special and ">defaults<" not in special
        assert "$25" in special and "$7.5" in special                    # its limit and its own cloud cap
        # A service account's defaults are its own: waived is normal, limited is not.
        _user(db_session, "bot", is_service_account=True, concurrency_waived=True)
        _user(db_session, "slowbot", is_service_account=True, concurrency_waived=False, acts_for_users=True)
        apps = _panel(client, admin_user, kind="apps")
        assert "um-chip" not in _row(apps, "bot")
        assert "um-chip-waive" in _row(apps, "slowbot") and "um-chip-onbehalf" in _row(apps, "slowbot")

    def test_apps_list_only_their_own_owners(self, client, db_session, admin_user, test_user):
        bot = _user(db_session, "app_bot", is_service_account=True)
        other_owner = _user(db_session, "carol")
        _user(db_session, "app_other", is_service_account=True)
        db_session.add(AppOwner(app_id=bot.id, owner_id=test_user.id))
        db_session.add(AppOwner(app_id=bot.id, owner_id=other_owner.id))
        db_session.commit()
        body = _panel(client, admin_user, kind="apps")
        assert "carol, testuser" in _row(body, "app_bot")
        assert ">nobody<" in _row(body, "app_other") and "carol" not in _row(body, "app_other")


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
        with patch("app.routers.admin.page.local_month_start", return_value=date(2026, 3, 1)):
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
        _user(db_session, "app_bot", is_service_account=True)
        body = client.get("/admin", headers=_hdr(admin_user)).text
        assert 'data-um-panel="users"' in body and 'data-um-panel="apps"' in body
        assert 'data-um-count="apps">1<' in body


class TestPageSize:
    def test_the_select_offers_the_sizes_and_keeps_the_choice(self, client, db_session, admin_user):
        for i in range(20):
            _user(db_session, f"p{i:02d}")
        body = client.get("/admin", headers=_hdr(admin_user)).text
        assert '<option value="15" selected' in body and '<option value="50"' in body
        assert "1-15 of" in body
        body = client.get("/admin", params={"limit": 50}, headers=_hdr(admin_user)).text
        assert '<option value="50" selected' in body and "1-22 of 22" in body
        # A size from a hand-written URL is still shown as what is selected.
        assert '<option value="7" selected' in client.get("/admin", params={"limit": 7}, headers=_hdr(admin_user)).text


class TestBulkUpdate:
    """POST /admin/users/bulk-update: one change, with its value named, for
    every account ticked in the table."""

    def _post(self, client, admin, accounts, **data):
        data["ids"] = ",".join(str(a.id) for a in accounts)
        return client.post("/admin/users/bulk-update", data=data, headers={**_hdr(admin), **JSON})

    def _three(self, session):
        return [_user(session, name, daily_limit_usd=10) for name in ("ann", "ben", "cat")]

    def test_admins_only(self, client, db_session, test_user):
        resp = client.post("/admin/users/bulk-update", data={"ids": str(test_user.id), "action": "azure", "enabled": "on"},
                           headers=web_auth_header(sub=test_user.username, scopes=["read"]))
        assert resp.status_code == 403

    def test_the_daily_limit(self, client, db_session, admin_user):
        people = self._three(db_session)
        people[1].daily_limit_usd = 25
        db_session.add(people[1])
        db_session.commit()
        resp = self._post(client, admin_user, people, action="limit", value="25")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "action": "limit", "changed": 2, "unchanged": 1, "skipped": []}
        for person in people:
            db_session.refresh(person)
            assert person.daily_limit_usd == 25

    def test_zero_is_a_zero_budget_and_the_cloud_cap_can_follow_the_default(self, client, db_session, admin_user):
        people = self._three(db_session)
        assert self._post(client, admin_user, people, action="limit", value="0").json()["changed"] == 3
        assert self._post(client, admin_user, people, action="cloud-limit", value="4.5").json()["changed"] == 3
        for person in people:
            db_session.refresh(person)
            assert (person.daily_limit_usd, person.cloud_daily_limit_usd) == (0, 4.5)
        assert self._post(client, admin_user, people, action="cloud-limit", value="").json()["changed"] == 3
        for person in people:
            db_session.refresh(person)
            assert person.cloud_daily_limit_usd is None

    @pytest.mark.parametrize("action,field", [
        ("azure", "can_use_azure"), ("bedrock", "can_use_bedrock"), ("waive", "concurrency_waived"),
        ("disable", "is_disabled"),
    ])
    def test_a_switch_takes_a_value_never_a_flip(self, client, db_session, admin_user, action, field):
        people = self._three(db_session)
        setattr(people[0], field, True)   # already on: a flip would turn it off
        db_session.add(people[0])
        db_session.commit()
        body = self._post(client, admin_user, people, action=action, enabled="on").json()
        assert (body["changed"], body["unchanged"]) == (2, 1)
        for person in people:
            db_session.refresh(person)
            assert getattr(person, field) is True
        assert self._post(client, admin_user, people, action=action, enabled="off").json()["changed"] == 3
        for person in people:
            db_session.refresh(person)
            assert getattr(person, field) is False

    def test_nothing_is_changed_by_a_bad_request(self, client, db_session, admin_user):
        people = self._three(db_session)
        bad = [
            dict(action="limit", value="-1"), dict(action="limit", value=""), dict(action="limit", value="nan"),
            dict(action="azure"), dict(action="azure", enabled="yes"), dict(action="delete", enabled="on"),
            dict(action="is_admin", enabled="on"), dict(action=""),
        ]
        for data in bad:
            assert self._post(client, admin_user, people, **data).status_code == 400, data
        for ids in ("", "x", "1;2", ",".join(str(i) for i in range(1, 300))):
            resp = client.post("/admin/users/bulk-update", data={"ids": ids, "action": "azure", "enabled": "on"},
                               headers={**_hdr(admin_user), **JSON})
            assert resp.status_code == 400, ids
        # One id that is gone refuses the whole change: the admin's table is out of date.
        resp = client.post("/admin/users/bulk-update",
                           data={"ids": f"{people[0].id},99999", "action": "azure", "enabled": "on"},
                           headers={**_hdr(admin_user), **JSON})
        assert resp.status_code == 400
        for person in people:
            db_session.refresh(person)
            assert (person.daily_limit_usd, person.can_use_azure, person.is_disabled) == (10, False, False)

    def test_you_are_skipped_when_disabling_and_told(self, client, db_session, admin_user):
        people = self._three(db_session)
        body = self._post(client, admin_user, [admin_user, *people], action="disable", enabled="on").json()
        assert body["changed"] == 3
        assert body["skipped"] == [{"username": "admin", "reason": "you cannot disable yourself"}]
        from sqlmodel import select

        db_session.expire_all()
        assert db_session.exec(select(User.is_disabled).where(User.username == "admin")).one() is False

    def test_overflow_is_for_people_and_off_takes_them_back_now(self, client, db_session, admin_user):
        people = self._three(db_session)
        bot = _user(db_session, "bot", is_service_account=True)
        with patch("app.routers.admin.users.overflow") as overflow:
            body = self._post(client, admin_user, [*people, bot], action="overflow", enabled="off").json()
        assert body["changed"] == 3
        assert body["skipped"] == [{"username": "bot", "reason": "a service account is never moved to the cloud"}]
        assert [call.args[1] for call in overflow.release_account.call_args_list] == ["ann", "ben", "cat"]
        overflow.refresh.assert_called_once()
        with patch("app.routers.admin.users.overflow") as overflow:
            assert self._post(client, admin_user, people, action="overflow", enabled="on").json()["changed"] == 3
        overflow.release_account.assert_not_called()

    def test_it_is_in_the_audit_log_with_the_accounts(self, client, db_session, admin_user):
        import json
        from sqlmodel import select

        people = self._three(db_session)
        self._post(client, admin_user, people, action="azure", enabled="on")
        db_session.expire_all()
        [row] = [r for r in db_session.exec(select(AuditLog)).all() if r.route == "/admin/users/bulk-update"]
        detail = json.loads(row.detail)
        assert row.actor == "admin" and row.status == 200
        assert detail["request"]["action"] == "azure" and detail["request"]["enabled"] == "on"
        assert detail["accounts"] == ["ann", "ben", "cat"] and detail["changed"] == 3

    def test_no_bulk_delete(self, client, db_session, admin_user):
        body = client.get("/admin", headers=_hdr(admin_user)).text
        bar = body[body.index('id="umBulk"'):]
        bar = bar[:bar.index("</select>")]
        assert 'value="limit"' in bar and 'value="azure:on"' in bar and 'value="disable:off"' in bar
        assert "delete" not in bar.lower()
