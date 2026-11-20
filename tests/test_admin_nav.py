"""Finding things in the admin panel: the one list of its sections
(``app/services/admin_nav.py``), the section bar and quick search built from
it (``_admin_tabs.html``) and the account search behind the quick search."""

from __future__ import annotations

import json
import re

from app.models.schema import User
from tests.conftest import web_auth_header


def _admin() -> dict:
    return web_auth_header(sub="admin", scopes=["admin"])


def _index(body: str) -> dict:
    raw = re.search(r'<script type="application/json" id="adminNavIndex">(.*?)</script>', body, re.S)
    assert raw, "no index on the page"
    return json.loads(raw.group(1))


def _panels(body: str) -> dict[str, str]:
    from app.services import admin_nav

    names = [tab for tab, _ in admin_nav.TABS]
    starts = [body.index(f'data-panel="{name}"') for name in names]
    assert starts == sorted(starts)
    ends = [*starts[1:], body.index("gwTabs('adminTabs'")]
    return {name: body[start:end] for name, start, end in zip(names, starts, ends)}


class TestTheListIsThePage:
    def test_every_section_is_an_element_of_its_tab(self, client, admin_user):
        from app.services import admin_nav

        panels = _panels(client.get("/admin", headers=_admin()).text)
        for section in admin_nav.SECTIONS:
            assert re.search(rf'id="{re.escape(section.id)}"[^>]*\bdata-section\b|\bdata-section\b[^>]*id="{re.escape(section.id)}"',
                             panels[section.tab]), f"{section.id} is not a section of the {section.tab} tab"

    def test_every_card_marked_as_a_section_is_in_the_list(self, client, admin_user):
        from app.services import admin_nav

        listed = {(s.tab, s.id) for s in admin_nav.SECTIONS}
        found = set()
        for tab, html in _panels(client.get("/admin", headers=_admin()).text).items():
            for tag in re.findall(r"<[a-z]+\b[^>]*\bdata-section\b[^>]*>", html):
                found.add((tab, re.search(r'id="([^"]+)"', tag).group(1)))
        assert found == listed
        assert len(found) >= 20   # the walk really found the cards

    def test_ids_are_unique_and_tabs_are_the_pages_tabs(self, client, admin_user):
        from app.services import admin_nav

        ids = [s.id for s in admin_nav.SECTIONS]
        assert len(ids) == len(set(ids))
        body = client.get("/admin", headers=_admin()).text
        for tab, _ in admin_nav.TABS:
            assert f'data-tab="{tab}"' in body
        assert {s.tab for s in admin_nav.SECTIONS} == {tab for tab, _ in admin_nav.TABS}

    def test_a_foldable_section_has_a_heading_to_carry_the_button(self, client, admin_user):
        from app.services import admin_nav

        body = client.get("/admin", headers=_admin()).text
        for section in admin_nav.SECTIONS:
            if not section.fold:
                continue
            start = body.index(f'id="{section.id}"')
            nxt = min((body.index(f'id="{s.id}"') for s in admin_nav.SECTIONS if body.index(f'id="{s.id}"') > start),
                      default=len(body))
            assert "<h2" in body[start:nxt], section.id


class TestTheIndex:
    def test_all_three_pages_carry_the_search(self, client, admin_user):
        for path in ("/admin", "/admin/models", "/admin/guide"):
            body = client.get(path, headers=_admin()).text
            assert 'id="adminSearchBtn"' in body, path
            index = _index(body)
            assert {s["id"] for s in index["sections"]} >= {"cloud-overflow", "client-rules", "user-management"}
        # Only /admin has the sections themselves, so only it has their bar.
        assert 'id="adminSections"' in client.get("/admin", headers=_admin()).text
        assert 'id="adminSections"' not in client.get("/admin/guide", headers=_admin()).text
        assert 'id="adminSections"' not in client.get("/admin/models", headers=_admin()).text

    def test_guide_entries_and_models_are_in_it(self, client, admin_user):
        from app.services import admin_guide

        index = _index(client.get("/admin", headers=_admin()).text)
        assert {g["id"] for g in index["guide"]} == {e.id for s in admin_guide.SECTIONS for e in s.entries}
        models = {(m["kind"], m["alias"]) for m in index["models"]}
        assert ("vllm", "test-llm") in models and ("azure", "azure-gpt-4") in models
        assert {m["kind"] for m in index["models"]} <= {"vllm", "azure", "bedrock"}
        assert {p["href"] for p in index["pages"]} == {"/admin/models", "/admin/guide", "/dashboard"}

    def test_a_broken_config_still_opens_the_search(self, client, admin_user):
        from unittest.mock import patch

        with patch("app.core.config.get_azure_models_snapshot", side_effect=RuntimeError("config")):
            body = client.get("/admin/guide", headers=_admin())
        assert body.status_code == 200 and _index(body.text)["models"] == []

    def test_it_holds_no_account_and_no_key(self, client, db_session, admin_user, test_user):
        body = client.get("/admin/guide", headers=_admin()).text
        assert test_user.api_key not in body and "testuser" not in json.dumps(_index(body))

    def test_jumps_inside_admin_go_through_the_one_function(self, client, admin_user):
        body = client.get("/admin", headers=_admin()).text
        assert "window.gwAdminReveal" in body
        # scrollIntoView on a card that may be folded, or on another tab, shows nothing.
        page = body[body.index('data-panel="overview"'):body.index("gwTabs('adminTabs'")]
        assert "scrollIntoView" not in page


class TestAccountSearch:
    def _search(self, client, q):
        resp = client.get("/admin/api/account-search", params={"q": q}, headers=_admin())
        assert resp.status_code == 200, resp.text
        return resp.json()["accounts"]

    def _people(self, session):
        session.add(User(username="alice", display_name="Alice Chen", org_code="RD-7", api_key="sk-alice-secret"))
        session.add(User(username="malice", display_name="Mal", org_code="QA-2", is_disabled=True))
        session.add(User(username="billing-etl", is_service_account=True))
        session.commit()

    def test_people_and_service_accounts_by_name_or_department(self, client, db_session, admin_user):
        self._people(db_session)
        found = self._search(client, "ali")
        assert [a["username"] for a in found] == ["alice", "malice"]     # a name that starts with it first
        assert found[0] == {
            "id": found[0]["id"], "username": "alice", "display_name": "Alice Chen", "org_code": "RD-7",
            "disabled": False, "service_account": False,
        }
        assert found[1]["disabled"] is True
        assert [a["username"] for a in self._search(client, "chen")] == ["alice"]
        assert [a["username"] for a in self._search(client, "qa-2")] == ["malice"]
        [etl] = self._search(client, "billing")
        assert etl["service_account"] is True

    def test_nothing_for_a_letter_and_never_a_key(self, client, db_session, admin_user):
        self._people(db_session)
        assert self._search(client, "") == [] and self._search(client, "a") == [] and self._search(client, " a ") == []
        resp = client.get("/admin/api/account-search", params={"q": "alice"}, headers=_admin())
        assert "sk-alice-secret" not in resp.text and "api_key" not in resp.text

    def test_at_most_a_handful(self, client, db_session, admin_user):
        from app.routers.admin.users import ACCOUNT_SEARCH_LIMIT

        for i in range(ACCOUNT_SEARCH_LIMIT + 5):
            db_session.add(User(username=f"worker{i:02d}"))
        db_session.commit()
        assert len(self._search(client, "worker")) == ACCOUNT_SEARCH_LIMIT

    def test_admins_only(self, client, test_user):
        resp = client.get("/admin/api/account-search", params={"q": "test"},
                          headers=web_auth_header(sub=test_user.username, scopes=["read"]))
        assert resp.status_code == 403
