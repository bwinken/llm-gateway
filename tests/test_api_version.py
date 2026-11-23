"""``GET /v1/version``: the API versions the gateway serves and the status
of each, for a frontend or a tool that is released apart from it.

No key and no sign-in: it is the first call such a client makes, before it
has either. The list is code (``app/core/api_versions.py``) and is checked
when the module is imported, so a list a client could not rely on stops the
start."""

from __future__ import annotations

import pytest

from app.core import api_versions
from app.core.api_versions import DEPRECATED, NIGHTLY, STABLE, ApiVersion
from tests.conftest import auth_header

# app.routers.* are imported inside the tests, never at module level.


def _no_nulls(value) -> bool:
    if isinstance(value, dict):
        return all(_no_nulls(v) for v in value.values())
    if isinstance(value, list):
        return all(_no_nulls(v) for v in value)
    return value is not None


class TestEndpoint:
    def test_answers_without_a_key_or_a_sign_in(self, client):
        resp = client.get("/v1/version")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _no_nulls(body), body
        assert body["object"] == "version"
        assert body["gateway"] == api_versions.GATEWAY_VERSION
        assert body["statuses"] == ["stable", "deprecated", "nightly"]
        # What the gateway serves today: one version, the one to build against.
        assert body["stable"] == "v1"
        assert {"version": "v1", "status": "stable"} in body["versions"]

    def test_the_stable_one_is_in_the_list_and_every_status_is_known(self, client):
        body = client.get("/v1/version").json()
        by_name = {v["version"]: v for v in body["versions"]}
        assert by_name[body["stable"]]["status"] == "stable"
        assert {v["status"] for v in body["versions"]} <= set(body["statuses"])

    def test_a_key_changes_nothing(self, client, test_user):
        assert client.get("/v1/version", headers=auth_header()).json() == client.get("/v1/version").json()
        # A wrong key is not looked at either: there is nothing to guess here.
        assert client.get("/v1/version", headers=auth_header("sk-nope")).status_code == 200

    def test_it_says_nothing_about_the_site(self, client):
        text = client.get("/v1/version").text
        for word in ("test-llm", "mock-", "testuser", "sk-"):
            assert word not in text

    def test_only_under_v1(self, client):
        # No SDK calls it, so it has no alias without the prefix.
        assert client.get("/version").status_code == 404

    def test_it_is_in_the_docs_in_one_group(self, client):
        operation = client.get("/openapi.json").json()["paths"]["/v1/version"]["get"]
        assert operation["tags"] == ["health"]
        assert "security" not in operation

    def test_the_real_app_serves_it_and_reports_the_same_release(self):
        from app.main import app

        assert app.version == api_versions.GATEWAY_VERSION
        assert "/v1/version" in app.openapi()["paths"]


class TestTheRelease:
    """``gateway`` in the answer: the release number, written once."""

    def test_it_is_the_version_in_pyproject(self):
        import re
        import tomllib
        from pathlib import Path

        with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as f:
            written = tomllib.load(f)["project"]["version"]
        assert api_versions.GATEWAY_VERSION == written
        assert re.fullmatch(r"\d+\.\d+\.\d+", written), written

    def test_the_lock_file_was_refreshed_with_it(self):
        """``uv sync --locked`` (CI, the image) refuses a lock file that
        names another version of the project: a bump is pyproject.toml and
        ``uv lock`` together."""
        import re
        from pathlib import Path

        lock = (Path(__file__).resolve().parent.parent / "uv.lock").read_text(encoding="utf-8")
        found = re.search(r'name = "llm-gateway"\nversion = "([^"]+)"', lock)
        assert found and found.group(1) == api_versions.GATEWAY_VERSION

    def test_a_missing_file_never_stops_the_start(self, tmp_path, monkeypatch):
        monkeypatch.setattr(api_versions, "_PYPROJECT", tmp_path / "nothing-here.toml")
        assert api_versions._release() == "0.0.0"


class TestPayload:
    """The shape for the days there is more than one version."""

    VERSIONS = (
        ApiVersion("v1", DEPRECATED, sunset="2027-06-30", successor="v2"),
        ApiVersion("v2", STABLE, note="Dashboard API reworked."),
        ApiVersion("v3", NIGHTLY),
    )

    def test_a_deprecated_version_says_until_when_and_what_next(self):
        api_versions.check(self.VERSIONS)
        body = api_versions.payload(self.VERSIONS)
        assert _no_nulls(body)
        assert body["stable"] == "v2"
        assert body["versions"] == [
            {"version": "v1", "status": "deprecated", "sunset": "2027-06-30", "successor": "v2"},
            {"version": "v2", "status": "stable", "note": "Dashboard API reworked."},
            {"version": "v3", "status": "nightly"},
        ]


class TestTheListIsChecked:
    def test_the_list_in_the_code_passes(self):
        api_versions.check()

    @pytest.mark.parametrize("versions, why", [
        ((), "exactly one version is stable"),
        ((ApiVersion("v1", NIGHTLY),), "exactly one version is stable"),
        ((ApiVersion("v1", STABLE), ApiVersion("v2", STABLE)), "exactly one version is stable"),
        ((ApiVersion("v1", STABLE), ApiVersion("v1", NIGHTLY)), "listed twice"),
        ((ApiVersion("v1", STABLE), ApiVersion("v2", "beta")), "unknown status"),
        ((ApiVersion("v1", DEPRECATED, successor="v2"), ApiVersion("v2", STABLE)), "sunset date"),
        ((ApiVersion("v1", DEPRECATED, sunset="next year", successor="v2"), ApiVersion("v2", STABLE)), "sunset date"),
        ((ApiVersion("v1", DEPRECATED, sunset="2027-06-30"), ApiVersion("v2", STABLE)), "version to move to"),
        ((ApiVersion("v1", DEPRECATED, sunset="2027-06-30", successor="v9"), ApiVersion("v2", STABLE)), "version to move to"),
        ((ApiVersion("v1", STABLE, sunset="2027-06-30"),), "only a deprecated version"),
    ])
    def test_a_list_a_client_could_not_rely_on_is_refused(self, versions, why):
        with pytest.raises(ValueError, match=why):
            api_versions.check(versions)

    def test_the_statuses_are_code(self):
        # A status is a promise about the code: there is no setting for it.
        import inspect

        source = inspect.getsource(api_versions)
        assert "config" not in source and "environ" not in source


class TestOneDocsPage:
    def test_redoc_and_the_oauth_redirect_page_are_gone(self):
        from fastapi.routing import iter_route_contexts

        from app.main import app

        paths = {context.path for context in iter_route_contexts(app.routes)}
        assert "/docs" in paths and "/openapi.json" in paths
        assert "/redoc" not in paths and "/docs/oauth2-redirect" not in paths
