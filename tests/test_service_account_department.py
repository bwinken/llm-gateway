"""A service account's department, and what its owners see of it.

An admin says which department a service account is for (its ``org_code``).
An owner sees the account, its key and its usage only while their own
department is that one (``schema.in_department``); the assignment itself
stays, and the admin is told who to look at. An owner also sees who the
account sent requests for, by backend (``stats.get_on_behalf_users``)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.models.schema import AppOwner, UsageLog, User, in_department
from app.services import stats
from tests.conftest import auth_header, earlier_today, web_auth_header

# app.routers.* are imported inside the tests, never at module level.

_JSON = {"Accept": "application/json"}


def _admin() -> dict[str, str]:
    return {**web_auth_header(sub="admin", scopes=["admin"]), **_JSON}


def _add(db_session: Session, **fields) -> User:
    user = User(**fields)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _move(db_session: Session, user: User, org_code: str) -> None:
    user.org_code = org_code
    db_session.add(user)
    db_session.commit()


@pytest.fixture()
def owner(db_session, test_user) -> User:
    """``testuser``, in department RD1."""
    _move(db_session, test_user, "RD1")
    return test_user


@pytest.fixture()
def hr_chat(db_session, owner) -> User:
    """A service account for RD1 that acts for its users, owned by ``owner``."""
    account = _add(
        db_session, username="hr-chat", api_key="sk-hr-chat-secret", is_service_account=True,
        acts_for_users=True, concurrency_waived=True, org_code="RD1",
    )
    db_session.add(AppOwner(app_id=account.id, owner_id=owner.id))
    db_session.commit()
    return account


def _sent_for(db_session, user: User, via: User, cost: str, backend: str = "vllm", tokens: int = 1000) -> None:
    db_session.add(UsageLog(
        user_id=user.id, model="test-llm", model_type="llm", input_tokens=tokens, output_tokens=tokens // 2,
        cost_usd=Decimal(cost), endpoint="/v1/chat/completions", backend=backend, via_user_id=via.id,
        created_at=earlier_today(),
    ))
    db_session.commit()


def _dashboard(client, sub: str = "testuser") -> str:
    resp = client.get("/dashboard", headers=web_auth_header(sub=sub))
    assert resp.status_code == 200, resp.text[:300]
    return resp.text


def _by_key(client) -> list[str]:
    body = client.get("/v1/service-accounts", headers=auth_header()).json()
    return [a["username"] for a in body["service_accounts"]]


# ── Who sees it ──

class TestInDepartment:
    def test_the_one_test(self):
        account = User(username="svc", is_service_account=True, org_code="RD1")
        assert in_department(User(username="a", org_code="RD1"), account)
        assert in_department(User(username="a", org_code=" rd1 "), account)  # as the IP allowlist compares org codes
        assert not in_department(User(username="a", org_code="RD2"), account)
        assert not in_department(User(username="a", org_code=""), account)
        # No department on the account: owners are not checked.
        assert in_department(User(username="a", org_code="RD2"), User(username="svc", is_service_account=True))

    def test_an_owner_in_the_department_sees_it(self, client, hr_chat):
        page = _dashboard(client)
        assert 'data-service-account="hr-chat"' in page and "data-service-withheld" not in page
        assert "department <span" in page and "RD1" in page
        assert _by_key(client) == ["hr-chat"]
        [account] = client.get("/v1/service-accounts", headers=auth_header()).json()["service_accounts"]
        assert account["department"] == "RD1"

    def test_an_owner_who_moved_sees_nothing_of_it(self, client, db_session, owner, hr_chat):
        _move(db_session, owner, "QA2")
        page = _dashboard(client)
        # Named, with the reason, and nothing else of it: no card, no key anywhere in the page.
        notice = page[page.index("data-service-withheld"):]
        notice = notice[:notice.index("</p>")]
        assert "hr-chat" in notice and "RD1" in notice
        assert 'data-service-account="hr-chat"' not in page
        assert "sk-hr-chat-secret" not in page and "hr-chat-secret" not in page
        assert _by_key(client) == []
        assert stats.get_withheld_apps(db_session, owner.id) == [{"username": "hr-chat", "department": "RD1"}]

    def test_they_cannot_reissue_its_key(self, client, db_session, owner, hr_chat):
        _move(db_session, owner, "QA2")
        resp = client.post(f"/dashboard/app/{hr_chat.id}/refresh-key", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 403 and "department" in resp.json()["detail"]
        db_session.expire_all()
        assert db_session.get(User, hr_chat.id).api_key == "sk-hr-chat-secret"

    def test_the_assignment_stays_and_comes_back_with_them(self, client, db_session, owner, hr_chat):
        _move(db_session, owner, "QA2")
        _dashboard(client)
        _by_key(client)
        assert db_session.exec(select(AppOwner).where(AppOwner.app_id == hr_chat.id)).all()  # only an admin ends one
        _move(db_session, owner, "RD1")
        assert 'data-service-account="hr-chat"' in _dashboard(client) and _by_key(client) == ["hr-chat"]

    def test_an_account_without_a_department_is_shown_as_before(self, client, db_session, owner, hr_chat):
        _move(db_session, hr_chat, "")
        _move(db_session, owner, "QA2")
        assert 'data-service-account="hr-chat"' in _dashboard(client) and _by_key(client) == ["hr-chat"]
        resp = client.post(f"/dashboard/app/{hr_chat.id}/refresh-key", headers=web_auth_header(sub="testuser"))
        assert resp.status_code == 200

    def test_someone_with_only_withheld_accounts_is_still_told(self, client, db_session, owner, hr_chat):
        _move(db_session, owner, "QA2")
        page = _dashboard(client)
        assert 'data-tab="service"' in page and 'data-panel="service"' in page


# ── The admin sets it ──

class TestAdminSetsIt:
    def _set(self, client, account: User, value: str):
        return client.post(f"/admin/users/{account.id}/department", data={"org_code": value}, headers=_admin())

    def test_a_department_people_are_in(self, client, db_session, admin_user, owner):
        batch = _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        # Spelled as the people in it have it.
        assert self._set(client, batch, " rd1 ").json() == {"ok": True, "org_code": "RD1", "outside": []}
        db_session.expire_all()
        assert db_session.get(User, batch.id).org_code == "RD1"
        assert self._set(client, batch, "").json() == {"ok": True, "org_code": "", "outside": []}

    def test_what_is_refused(self, client, db_session, admin_user, owner, hr_chat):
        resp = self._set(client, hr_chat, "RD9")  # nobody is in it: a typing mistake would hide it from everyone
        assert resp.status_code == 400 and "RD9" in resp.json()["detail"]
        # A person's department is their sign-in's.
        assert self._set(client, owner, "RD1").status_code == 400
        assert client.patch(f"/admin/users/{owner.id}", json={"org_code": "RD1"}, headers=_admin()).status_code == 400
        assert client.post(f"/admin/users/{hr_chat.id}/department", data={"org_code": ""},
                           headers=web_auth_header(sub="testuser")).status_code == 403
        db_session.expire_all()
        assert db_session.get(User, hr_chat.id).org_code == "RD1"

    def test_the_owners_stay_and_the_admin_is_told_who_is_outside(self, client, db_session, admin_user, owner, hr_chat):
        _add(db_session, username="quinn", api_key="sk-quinn", org_code="QA2")
        assert self._set(client, hr_chat, "QA2").json() == {"ok": True, "org_code": "QA2", "outside": ["testuser"]}
        assert db_session.exec(select(AppOwner).where(AppOwner.app_id == hr_chat.id)).one().owner_id == owner.id

    def test_a_new_owner_must_be_in_it(self, client, db_session, admin_user, owner, hr_chat):
        _add(db_session, username="quinn", api_key="sk-quinn", org_code="QA2")
        _add(db_session, username="rosa", api_key="sk-rosa", org_code="RD1")
        resp = client.post(f"/admin/users/{hr_chat.id}/owner", data={"owner_usernames": "testuser,quinn"}, headers=_admin())
        assert resp.status_code == 400 and "quinn (QA2)" in resp.json()["detail"]
        resp = client.post(f"/admin/users/{hr_chat.id}/owner", data={"owner_usernames": "testuser,rosa"}, headers=_admin())
        assert resp.json()["owners"] == ["testuser", "rosa"]

    def test_an_owner_who_moved_is_never_dropped_by_an_edit(self, client, db_session, admin_user, owner, hr_chat):
        _add(db_session, username="rosa", api_key="sk-rosa", org_code="RD1")
        _move(db_session, owner, "QA2")
        resp = client.post(f"/admin/users/{hr_chat.id}/owner", data={"owner_usernames": "testuser,rosa"}, headers=_admin())
        assert resp.status_code == 200 and resp.json()["owners"] == ["testuser", "rosa"]

    def test_given_when_the_account_is_created(self, client, db_session, admin_user, owner):
        _add(db_session, username="quinn", api_key="sk-quinn", org_code="QA2")
        data = {"username": "billing-etl", "org_code": "rd1", "owner_usernames": "quinn"}
        resp = client.post("/admin/users/create", data=data, headers=_admin())
        assert resp.status_code == 400  # refused before anything is created
        assert db_session.exec(select(User).where(User.username == "billing-etl")).first() is None
        resp = client.post("/admin/users/create", data={**data, "owner_usernames": "testuser"}, headers=_admin())
        assert resp.status_code == 200, resp.text
        assert db_session.exec(select(User).where(User.username == "billing-etl")).one().org_code == "RD1"

    def test_the_json_api(self, client, db_session, admin_user, owner):
        resp = client.post("/admin/users", json={"username": "ocr", "is_service_account": True, "org_code": "RD1"},
                           headers=_admin())
        assert resp.status_code == 200, resp.text
        assert client.post("/admin/users", json={"username": "bob", "org_code": "RD1"}, headers=_admin()).status_code == 400
        ocr = db_session.exec(select(User).where(User.username == "ocr")).one()
        assert client.patch(f"/admin/users/{ocr.id}", json={"org_code": "nowhere"}, headers=_admin()).status_code == 400
        assert client.patch(f"/admin/users/{ocr.id}", json={"org_code": ""}, headers=_admin()).status_code == 200
        db_session.expire_all()
        assert db_session.get(User, ocr.id).org_code == ""

    def test_the_drawer_has_the_field(self, client, db_session, admin_user, owner, hr_chat):
        drawer = client.get(f"/admin/users/{hr_chat.id}/detail", headers=_admin()).text
        assert f'action="/admin/users/{hr_chat.id}/department"' in drawer and 'name="org_code" value="RD1"' in drawer
        assert '<option value="RD1">' in drawer
        assert "/department" not in client.get(f"/admin/users/{owner.id}/detail", headers=_admin()).text


# ── The admin is told ──

class TestTheAdminIsTold:
    def test_an_owner_outside_the_department(self, client, db_session, admin_user, owner, hr_chat):
        assert stats.owners_to_review(db_session) == []
        page = client.get("/admin", headers=web_auth_header(sub="admin", scopes=["admin"])).text
        assert "ownerReviewAttention" not in page
        _move(db_session, owner, "QA2")
        assert stats.owners_to_review(db_session) == [{
            "id": hr_chat.id, "username": "hr-chat", "department": "RD1",
            "owners": [{"username": "testuser", "org_code": "QA2", "reason": "department"}],
        }]
        page = client.get("/admin", headers=web_auth_header(sub="admin", scopes=["admin"])).text
        assert "1 service account owner to review" in page
        item = page[page.index('id="ownerReviewAttention"'):]
        item = item[:item.index("</button>")]
        assert "hr-chat" in item and "testuser" in item and "is in QA2" in item
        assert f'data-um-detail="{hr_chat.id}"' in item  # it opens the account

    def test_a_disabled_owner(self, db_session, owner, hr_chat):
        owner.is_disabled = True
        db_session.add(owner)
        db_session.commit()
        [entry] = stats.owners_to_review(db_session)
        assert entry["owners"] == [{"username": "testuser", "org_code": "RD1", "reason": "disabled"}]

    def test_it_clears_itself(self, client, db_session, admin_user, owner, hr_chat):
        _move(db_session, owner, "QA2")
        assert stats.owners_to_review(db_session)
        client.post(f"/admin/users/{hr_chat.id}/owner", data={"owner_usernames": ""}, headers=_admin())
        assert stats.owners_to_review(db_session) == []

    def test_the_table_filter_and_the_drawer_say_who(self, client, db_session, admin_user, owner, hr_chat):
        _add(db_session, username="batch", api_key="sk-batch", is_service_account=True)
        table = lambda: client.get(  # noqa: E731
            "/admin/user-table", params={"kind": "apps", "status": "owner_review"},
            headers=web_auth_header(sub="admin", scopes=["admin"]),
        ).text
        assert 'data-username="hr-chat"' not in table()
        _move(db_session, owner, "QA2")
        body = table()
        assert 'data-username="hr-chat"' in body and 'data-username="batch"' not in body
        assert "data-owner-review" in body and "1 to review" in body
        drawer = client.get(f"/admin/users/{hr_chat.id}/detail", headers=_admin()).text
        issues = drawer[drawer.index("data-ud-owner-issues"):]
        assert "testuser" in issues[:400] and "QA2" in issues[:400]
        # Never a filter for people: a person has no owners.
        people = client.get("/admin/user-table", params={"kind": "users", "status": "owner_review"},
                            headers=web_auth_header(sub="admin", scopes=["admin"])).text
        assert 'data-username="testuser"' not in people


# ── The IP allowlist is as it was ──

class TestTheAllowlistDoesNotReadIt:
    def test_a_department_rule_admits_its_people_not_its_service_accounts(self, db_session, owner, hr_chat):
        from unittest.mock import MagicMock, patch

        from fastapi import HTTPException

        from app.services import ip_allowlist

        ip_allowlist.add_rule(db_session, "203.0.113.0/24", created_by="admin", org_codes=["RD1"])
        ip_allowlist.refresh()
        request = MagicMock()
        request.client.host = "203.0.113.5"
        request.url.path = "/v1/models"
        with patch("app.services.ip_allowlist.get_ip_allowlist_settings", return_value={"mode": "enforce"}):
            ip_allowlist.check(request, owner)  # a person of RD1: admitted
            with pytest.raises(HTTPException) as refused:
                ip_allowlist.check(request, hr_chat)  # the account for RD1: not by that rule
            assert refused.value.status_code == 403
            ip_allowlist.add_rule(db_session, "203.0.113.0/24", created_by="admin", usernames=["hr-chat"])
            ip_allowlist.refresh()
            ip_allowlist.check(request, hr_chat)  # by name, as before


# ── Who it was used for ──

class TestWhoItWasUsedFor:
    def _people(self, db_session, n: int) -> list[User]:
        return [
            _add(db_session, username=f"user{i:02d}", api_key=f"sk-user{i:02d}", display_name=f"User {i}", org_code="RD1")
            for i in range(n)
        ]

    def test_by_user_and_backend(self, db_session, hr_chat):
        alice, bob = self._people(db_session, 2)
        _sent_for(db_session, alice, hr_chat, "2")
        _sent_for(db_session, alice, hr_chat, "1", backend="azure")
        _sent_for(db_session, bob, hr_chat, "5")
        flow = stats.get_on_behalf_users(db_session, [hr_chat.id])[hr_chat.id]
        assert flow["users"] == 2
        assert [(r["user"], r["backend"], r["cost_usd"], r["requests"]) for r in flow["rows"]] == [
            ("user01", "vllm", 5.0, 1), ("user00", "vllm", 2.0, 1), ("user00", "azure", 1.0, 1),
        ]
        # It adds up to what the card already says went through the account.
        assert sum(r["cost_usd"] for r in flow["rows"]) == stats.get_on_behalf_month(db_session)[hr_chat.id]["cost"]
        assert stats.get_on_behalf_users(db_session, []) == {}

    def test_the_largest_by_name_and_the_rest_together(self, db_session, hr_chat):
        people = self._people(db_session, stats.FLOW_TOP_USERS + 3)
        for i, person in enumerate(people):
            _sent_for(db_session, person, hr_chat, str(i + 1))
        _sent_for(db_session, people[0], hr_chat, "0.5", backend="azure")  # among the rest, on another backend
        flow = stats.get_on_behalf_users(db_session, [hr_chat.id])[hr_chat.id]
        named = [r for r in flow["rows"] if r["user"]]
        rest = [r for r in flow["rows"] if not r["user"]]
        assert flow["users"] == 13 and len({r["user"] for r in named}) == stats.FLOW_TOP_USERS
        assert named[0]["user"] == "user12" and "user00" not in {r["user"] for r in named}
        assert sorted((r["backend"], r["cost_usd"], r["requests"], r["others"]) for r in rest) == [
            ("azure", 0.5, 1, 3), ("vllm", 6.0, 3, 3),  # users 00, 01, 02: $1 + $2 + $3
        ]
        assert sum(r["cost_usd"] for r in flow["rows"]) == 91.5

    def test_where_the_cost_is_the_same_the_tokens_rank(self, db_session, hr_chat):
        light, heavy = self._people(db_session, 2)
        _sent_for(db_session, light, hr_chat, "0", tokens=100)   # on-prem priced at $0
        _sent_for(db_session, heavy, hr_chat, "0", tokens=9000)
        flow = stats.get_on_behalf_users(db_session, [hr_chat.id], top=1)[hr_chat.id]
        assert [r["user"] for r in flow["rows"]] == ["user01", ""]

    def test_a_deleted_user_keeps_their_place(self, db_session, hr_chat):
        [gone] = self._people(db_session, 1)
        _sent_for(db_session, gone, hr_chat, "1")
        uid = gone.id
        db_session.delete(gone)
        db_session.commit()
        assert stats.get_on_behalf_users(db_session, [hr_chat.id])[hr_chat.id]["rows"][0]["user"] == f"#{uid}"

    def test_the_owners_card_draws_it(self, client, db_session, hr_chat):
        alice, bob = self._people(db_session, 2)
        alice.display_name = "<b>Alice</b>"
        db_session.add(alice)
        db_session.commit()
        _sent_for(db_session, alice, hr_chat, "2")
        _sent_for(db_session, bob, hr_chat, "5", backend="azure")
        page = _dashboard(client)
        card = page[page.index('data-service-account="hr-chat"'):]
        flow = card[card.index("data-service-flow"):card.index("data-service-howto=")]
        assert 'data-flow-user="user00"' in flow and 'data-flow-user="user01"' in flow
        assert "2 users this month" in flow and "$5.00" in flow
        # A name is what a person's sign-in said: shown as text.
        assert "&lt;b&gt;Alice&lt;/b&gt;" in flow and "<b>Alice</b>" not in flow
        data = flow[flow.index("data-flow-rows='") + len("data-flow-rows='"):]
        rows = json.loads(data[:data.index("'")].replace("&#34;", '"').replace("&lt;", "<").replace("&gt;", ">"))
        assert [(r["user"], r["backend"], r["cost_usd"]) for r in rows] == [("user01", "azure", 5.0), ("user00", "vllm", 2.0)]

    def test_an_account_that_sent_nothing_for_anyone_has_none(self, client, db_session, hr_chat):
        assert "data-flow-rows='" not in _dashboard(client)

    def test_an_owner_outside_the_department_gets_none_of_it(self, client, db_session, owner, hr_chat):
        [alice] = self._people(db_session, 1)
        _sent_for(db_session, alice, hr_chat, "2")
        _move(db_session, owner, "QA2")
        page = _dashboard(client)
        assert "data-flow-rows='" not in page and "user00" not in page

    def test_it_is_not_given_by_api_key(self, client, db_session, hr_chat):
        # Who uses an application, and how much, stays behind the sign-in.
        [alice] = self._people(db_session, 1)
        _sent_for(db_session, alice, hr_chat, "2")
        body = client.get("/v1/service-accounts", headers=auth_header()).text
        assert "user00" not in body and "flow" not in body
