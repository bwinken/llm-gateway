"""Service accounts (formerly "app accounts"): accounts for programs.

What makes an account one is ``users.is_service_account``, not an ``app_``
prefix in its name, so a new one is named freely; the accounts and scripts
from before keep working. Nobody signs in under a service account's name.
Its owners see it on their dashboard's *Service accounts* tab, with this
month's usage per model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.models.schema import AppOwner, UsageLog, User
from tests.conftest import auth_header, earlier_today, web_auth_header


def _admin(admin_user: User) -> dict:
    return web_auth_header(sub=admin_user.username, scopes=["admin"])


def _get(db_session: Session, username: str) -> User | None:
    db_session.expire_all()
    return db_session.exec(select(User).where(User.username == username)).first()


def _service(db_session: Session, name: str, *owners: User, **fields) -> User:
    fields.setdefault("api_key", f"sk-{name}-key")
    account = User(username=name, is_service_account=True, concurrency_waived=True, **fields)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    for owner in owners:
        db_session.add(AppOwner(app_id=account.id, owner_id=owner.id))
    db_session.commit()
    return account


def _spend(db_session: Session, user: User, usd: str, model: str = "test-llm", backend: str = "vllm",
           model_type: str = "llm", **kw) -> None:
    kw.setdefault("created_at", earlier_today())
    db_session.add(UsageLog(
        user_id=user.id, model=model, model_type=model_type, input_tokens=1000, output_tokens=500,
        cost_usd=Decimal(usd), endpoint="/v1/chat/completions", backend=backend, **kw,
    ))
    db_session.commit()


class TestNaming:
    """The admin's two ways to make one: the form and the API."""

    def test_the_form_keeps_the_name_as_typed(self, client, db_session, admin_user, test_user):
        resp = client.post(
            "/admin/users/create",
            data={"username": "billing-etl", "daily_limit_usd": "5", "owner_username": test_user.username},
            headers=_admin(admin_user), follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _get(db_session, "app_billing-etl") is None  # no prefix is added any more
        account = _get(db_session, "billing-etl")
        assert account.is_service_account is True and account.concurrency_waived is True
        assert account.daily_limit_usd == 5.0
        owners = db_session.exec(select(AppOwner).where(AppOwner.app_id == account.id)).all()
        assert [o.owner_id for o in owners] == [test_user.id]

    def test_an_app_name_is_still_fine(self, client, db_session, admin_user):
        client.post("/admin/users/create", data={"username": "app_old_style"}, headers=_admin(admin_user),
                    follow_redirects=False)
        assert _get(db_session, "app_old_style").is_service_account is True
        assert _get(db_session, "app_app_old_style") is None

    @pytest.mark.parametrize("name", ["", "   ", "has space", "-leading", "a/b", "x" * 65, "引號'"])
    def test_names_the_form_refuses(self, client, db_session, admin_user, name):
        resp = client.post("/admin/users/create", data={"username": name}, headers=_admin(admin_user),
                           follow_redirects=False)
        assert resp.status_code == 400
        assert db_session.exec(select(User).where(User.is_service_account)).all() == []

    def test_a_taken_name_is_refused(self, client, db_session, admin_user, test_user):
        resp = client.post("/admin/users/create", data={"username": test_user.username},
                           headers=_admin(admin_user), follow_redirects=False)
        assert resp.status_code == 409
        assert _get(db_session, test_user.username).is_service_account is False

    def test_the_api_takes_the_kind(self, client, db_session, admin_user):
        resp = client.post("/admin/users", json={"username": "nightly-report", "is_service_account": True},
                           headers=_admin(admin_user))
        assert resp.status_code == 200 and resp.json()["is_service_account"] is True
        account = _get(db_session, "nightly-report")
        assert account.is_service_account is True and account.concurrency_waived is True

    def test_scripts_from_before_keep_working(self, client, db_session, admin_user):
        # Without the field, a name of the old form still makes a service
        # account, as it did when the name was the test; any other name a person.
        assert client.post("/admin/users", json={"username": "app_etl"}, headers=_admin(admin_user)).status_code == 200
        assert client.post("/admin/users", json={"username": "newhire"}, headers=_admin(admin_user)).status_code == 200
        assert _get(db_session, "app_etl").is_service_account is True
        person = _get(db_session, "newhire")
        assert person.is_service_account is False and person.concurrency_waived is False

    def test_the_field_wins_over_the_name(self, client, db_session, admin_user):
        client.post("/admin/users", json={"username": "app_person", "is_service_account": False},
                    headers=_admin(admin_user))
        assert _get(db_session, "app_person").is_service_account is False

    def test_the_field_is_a_boolean(self, client, db_session, admin_user):
        resp = client.post("/admin/users", json={"username": "x", "is_service_account": "yes"},
                           headers=_admin(admin_user))
        assert resp.status_code == 400 and _get(db_session, "x") is None

    def test_the_listing_says_which_is_which(self, client, db_session, admin_user, test_user):
        _service(db_session, "billing-etl")
        rows = {u["username"]: u for u in client.get("/admin/users", headers=_admin(admin_user)).json()}
        assert rows["billing-etl"]["is_service_account"] is True
        assert rows[test_user.username]["is_service_account"] is False


class TestNoSignIn:
    """A service account's name is no longer set apart by a prefix, so a
    person whose AuthCenter username equals it must not become it."""

    def test_the_dashboard_refuses(self, client, db_session):
        _service(db_session, "billing-etl")
        resp = client.get("/dashboard", headers=web_auth_header(sub="billing-etl"))
        assert resp.status_code == 403 and "service account" in resp.json()["detail"]

    def test_a_token_gets_no_key(self, client, db_session):
        _service(db_session, "billing-etl")
        resp = client.post("/auth/api-key", headers=web_auth_header(sub="billing-etl"))
        assert resp.status_code == 403
        assert "sk-billing-etl-key" not in resp.text

    def test_its_key_works(self, client, db_session):
        _service(db_session, "billing-etl", daily_limit_usd=5.0)
        assert client.get("/v1/me", headers=auth_header("sk-billing-etl-key")).json()["username"] == "billing-etl"

    def test_people_named_like_an_old_app_account_still_sign_in(self, client, db_session):
        # Only the column decides: a first sign-in never makes a service account.
        resp = client.get("/dashboard", headers=web_auth_header(sub="app_person"))
        assert resp.status_code == 200
        assert _get(db_session, "app_person").is_service_account is False


class TestTreatedAsOne:
    """Everything that told app accounts from people does so by the column."""

    def test_the_admin_tables(self, client, db_session, admin_user, test_user):
        _service(db_session, "billing-etl")
        users = client.get("/admin/user-table", params={"kind": "users"}, headers=_admin(admin_user)).text
        service = client.get("/admin/user-table", params={"kind": "apps"}, headers=_admin(admin_user)).text
        assert 'data-username="billing-etl"' in service and 'data-username="billing-etl"' not in users
        assert 'data-username="testuser"' in users and 'data-username="testuser"' not in service
        assert "Service account" in service

    def test_not_counted_as_a_person(self, db_session, test_user):
        from app.services.stats import human_user_ids

        etl = _service(db_session, "billing-etl")
        assert etl.id not in human_user_ids(db_session) and test_user.id in human_user_ids(db_session)

    def test_listed_with_the_service_accounts_in_the_overview(self, db_session, test_user):
        from app.services.stats import get_monthly_overview

        etl = _service(db_session, "billing-etl")
        _spend(db_session, etl, "2")
        _spend(db_session, test_user, "1")
        overview = get_monthly_overview(db_session)
        assert [row["username"] for row in overview["app_leaderboard"]] == ["billing-etl"]
        assert [row["username"] for row in overview["user_leaderboard"]] == ["testuser"]

    def test_never_mailed(self, db_session, test_user):
        from app.models.schema import ClientViolation
        from app.services import client_rules

        _service(db_session, "billing-etl")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for name in ("billing-etl", test_user.username):
            db_session.add(ClientViolation(rule_id="CC001", username=name, first_seen_at=now, last_seen_at=now))
        db_session.commit()
        assert client_rules.mail_all_targets(db_session, only_unmailed=False) == [test_user.username]
        assert [name for name, _ in client_rules.due_list(db_session)] == []
        rows = client_rules.violations(db_session, "CC001")
        assert [v.username for v in client_rules.mailable(db_session, rows)] == [test_user.username]


class TestDashboardTab:
    """The owner's *Service accounts* tab."""

    def _page(self, client, user: User) -> str:
        resp = client.get("/dashboard", headers=web_auth_header(sub=user.username))
        assert resp.status_code == 200, resp.text
        return resp.text

    def _card(self, page: str, name: str) -> str:
        start = page.index(f'data-service-account="{name}"')
        end = page.find("data-service-account=", start + 1)
        return page[start:end if end > 0 else page.index("API examples", start)]

    def test_no_tab_without_one(self, client, db_session, test_user):
        _service(db_session, "someone-elses")
        page = self._page(client, test_user)
        assert 'data-tab="service"' not in page and 'data-panel="service"' not in page
        assert "someone-elses" not in page

    def test_a_tab_of_its_own(self, client, db_session, test_user):
        _service(db_session, "billing-etl", test_user)
        _service(db_session, "app_legacy", test_user)
        page = self._page(client, test_user)
        assert 'data-tab="service"' in page
        # Its own panel, after the usage one; the old table is gone.
        assert page.index('data-panel="usage"') < page.index('data-panel="service"')
        assert "My App Accounts" not in page
        # By name in natural order, whatever the name looks like.
        assert page.index('data-service-account="app_legacy"') < page.index('data-service-account="billing-etl"')

    def test_this_months_usage_per_model(self, client, db_session, test_user):
        etl = _service(db_session, "billing-etl", test_user)
        _spend(db_session, etl, "3", model="azure-gpt-4", backend="azure")
        _spend(db_session, etl, "0.5")
        _spend(db_session, etl, "0.5")
        last_month = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=5)).replace(tzinfo=None)
        _spend(db_session, etl, "40", model="old-model", created_at=last_month)
        card = self._card(self._page(client, test_user), "billing-etl")
        assert "$4.0000" in card                                   # the month's total
        assert card.index("test-llm") < card.index("azure-gpt-4")  # the one model order
        assert "On-Prem" in card and "Azure" in card
        assert "25.0%" in card and "75.0%" in card                 # each model's share of this account
        assert "old-model" not in card and "$40" not in card       # last month's is not this month's

    def test_an_account_without_usage_says_so(self, client, db_session, test_user):
        _service(db_session, "billing-etl", test_user)
        assert "No usage this month." in self._card(self._page(client, test_user), "billing-etl")

    def test_each_accounts_own_figures(self, client, db_session, test_user):
        a = _service(db_session, "etl-a", test_user)
        b = _service(db_session, "etl-b", test_user)
        _spend(db_session, a, "1", model="test-llm")
        _spend(db_session, b, "2", model="test-vlm", model_type="vlm")
        page = self._page(client, test_user)
        assert "test-vlm" not in self._card(page, "etl-a") and "$1.0000" in self._card(page, "etl-a")
        assert "test-llm" not in self._card(page, "etl-b") and "$2.0000" in self._card(page, "etl-b")

    def test_same_figures_as_the_api(self, client, db_session, test_user):
        etl = _service(db_session, "billing-etl", test_user)
        _spend(db_session, etl, "1.25", model="test-vlm", model_type="vlm")
        (app,) = client.get("/v1/apps", headers=auth_header()).json()["apps"]
        card = self._card(self._page(client, test_user), "billing-etl")
        for row in app["month"]["models"]:
            assert row["model"] in card and f"${row['cost_usd']:.4f}" in card

    def test_the_owner_regenerates_its_key(self, client, db_session, test_user, admin_user):
        etl = _service(db_session, "billing-etl", test_user)
        other = web_auth_header(sub=admin_user.username)
        assert client.post(f"/dashboard/app/{etl.id}/refresh-key", headers=other).status_code == 403
        resp = client.post(f"/dashboard/app/{etl.id}/refresh-key", headers=web_auth_header(sub=test_user.username))
        assert resp.status_code == 200
        assert resp.json()["api_key"] == _get(db_session, "billing-etl").api_key != "sk-billing-etl-key"
