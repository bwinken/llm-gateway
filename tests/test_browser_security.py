"""CORS policy and the cross-site request guard (app/core/browser_security.py).

The Web UI / admin authenticate with the browser's SSO cookie (nginx turns it
into the JWT these tests send directly), so another site must neither read
their responses (CORS without credentials) nor make the user change anything
(cross-site writes refused). API routes authenticate with a header key the
client sets itself and stay callable from any origin.
"""

from __future__ import annotations

from sqlmodel import Session

from app.models.schema import User
from tests.conftest import _build_test_app, auth_header, web_auth_header

JSON = {"Accept": "application/json"}
EVIL = "http://evil.example.com"
SIBLING = "http://intranet.testserver"   # same site as testserver, other host


def _admin_hdr(admin: User) -> dict:
    return web_auth_header(sub=admin.username, scopes=["admin"])


class TestCors:
    def test_cookie_carrying_read_is_not_shared_with_another_origin(self, client, admin_user, test_user):
        resp = client.get(
            f"/admin/users/{test_user.id}/api-key",
            headers={**_admin_hdr(admin_user), "Origin": EVIL, "Cookie": "_llm_gw_oauth2=x"},
        )
        # Starlette may still echo the Origin on a cookie-carrying request; what
        # keeps the response from the calling page is the missing
        # Access-Control-Allow-Credentials (a credentialed fetch requires "true").
        assert "access-control-allow-credentials" not in resp.headers

    def test_credentialed_preflight_is_not_approved(self, client):
        resp = client.options(
            "/admin/users/1/toggle-disable",
            headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"},
        )
        assert resp.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in resp.headers

    def test_api_preflight_from_any_origin_is_allowed_with_the_key_header(self, client):
        resp = client.options(
            "/v1/chat/completions",
            headers={
                "Origin": EVIL,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"
        assert "authorization" in resp.headers["access-control-allow-headers"].lower()
        assert "access-control-allow-credentials" not in resp.headers

    def test_api_call_from_another_origin_still_works(self, client):
        resp = client.get("/v1/me", headers={**auth_header(), "Origin": EVIL})
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"


class TestCrossSiteGuard:
    def _toggle(self, client, admin_user, test_user, extra: dict):
        return client.post(
            f"/admin/users/{test_user.id}/toggle-disable",
            headers={**_admin_hdr(admin_user), **JSON, **extra},
        )

    def _disabled(self, db_session: Session, user: User) -> bool:
        db_session.expire_all()
        return db_session.get(User, user.id).is_disabled

    def test_own_page_posts_go_through(self, client, db_session, admin_user, test_user):
        resp = self._toggle(client, admin_user, test_user,
                            {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 200
        assert self._disabled(db_session, test_user)

    def test_origin_matching_host_goes_through_without_fetch_metadata(self, client, admin_user, test_user):
        resp = self._toggle(client, admin_user, test_user, {"Origin": "http://testserver"})
        assert resp.status_code == 200

    def test_origin_port_is_ignored(self, client, admin_user, test_user):
        # nginx forwards $host (no port); the browser's Origin keeps it.
        resp = self._toggle(client, admin_user, test_user, {"Origin": "https://testserver:8443"})
        assert resp.status_code == 200

    def test_non_browser_clients_go_through(self, client, admin_user, test_user):
        assert self._toggle(client, admin_user, test_user, {}).status_code == 200

    def test_same_site_sibling_is_refused(self, client, db_session, admin_user, test_user):
        # SameSite=lax still sends the cookie from a sibling host: the case CORS can't stop.
        resp = self._toggle(client, admin_user, test_user,
                            {"Origin": SIBLING, "Sec-Fetch-Site": "same-site"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cross-site request refused."
        assert not self._disabled(db_session, test_user)

    def test_cross_site_is_refused(self, client, db_session, admin_user, test_user):
        resp = self._toggle(client, admin_user, test_user,
                            {"Origin": EVIL, "Sec-Fetch-Site": "cross-site"})
        assert resp.status_code == 403
        assert not self._disabled(db_session, test_user)

    def test_foreign_origin_without_fetch_metadata_is_refused(self, client, admin_user, test_user):
        assert self._toggle(client, admin_user, test_user, {"Origin": EVIL}).status_code == 403

    def test_null_origin_is_refused(self, client, admin_user, test_user):
        assert self._toggle(client, admin_user, test_user, {"Origin": "null"}).status_code == 403

    def test_dashboard_writes_are_guarded(self, client, db_session, test_user):
        before = db_session.get(User, test_user.id).api_key
        resp = client.post(
            "/dashboard/refresh-key",
            headers={**web_auth_header(sub=test_user.username), "Origin": EVIL, "Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code == 403
        db_session.expire_all()
        assert db_session.get(User, test_user.id).api_key == before

    def test_reads_are_not_guarded(self, client, admin_user):
        resp = client.get("/admin/user-table?kind=users", headers={**_admin_hdr(admin_user), "Sec-Fetch-Site": "cross-site"})
        assert resp.status_code == 200

    def test_api_writes_from_other_sites_are_not_guarded(self, client):
        # Browser-based API tools: authenticated by a header key, no ambient cookie.
        client.__httpx_mock__.post.side_effect = None
        resp = client.post(
            "/v1/tokenize",
            json={"model": "nonexistent", "prompt": "hi"},
            headers={**auth_header(), "Origin": EVIL, "Sec-Fetch-Site": "cross-site"},
        )
        assert resp.status_code != 403


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def test_every_cookie_authenticated_write_is_under_a_guarded_prefix():
    """A new SSO-authenticated POST/PUT/PATCH/DELETE outside /admin or /dashboard
    would skip the guard: add its prefix to COOKIE_AUTH_PREFIXES."""
    from fastapi.routing import APIRoute

    from app.core.auth import get_web_user
    from app.core.browser_security import SAFE_METHODS, is_cookie_auth_path

    app = _build_test_app()
    unguarded = [
        (sorted(r.methods - SAFE_METHODS), r.path)
        for r in app.routes
        if isinstance(r, APIRoute)
        and r.methods - SAFE_METHODS
        and _depends_on(r.dependant, get_web_user)
        and not is_cookie_auth_path(r.path)
    ]
    assert unguarded == []
    # The walk must actually see the SSO routes, or the check above proves nothing.
    assert any(
        isinstance(r, APIRoute) and r.path == "/dashboard/refresh-key" and _depends_on(r.dependant, get_web_user)
        for r in app.routes
    )
