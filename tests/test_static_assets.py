"""The pages load their stylesheet, Chart.js and fonts from this site only
(app/static, app/core/static_assets.py), under URLs that change with the
content, and say so to the browser with a Content-Security-Policy."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from tests.conftest import web_auth_header

PAGES = ["/", "/dashboard", "/admin", "/admin/models", "/setup"]
_STATIC_LINK = re.compile(r'(?:href|src)="(/static/[^"?]+)\?v=([0-9a-f]{12})"')


def _page(client, path):
    resp = client.get(path, headers=web_auth_header(sub="admin", scopes=["read", "admin"]))
    assert resp.status_code == 200, (path, resp.status_code)
    return resp


class TestNothingFromOutside:
    @pytest.mark.parametrize("path", PAGES)
    def test_no_cdn_and_every_asset_is_served_here(self, client, admin_user, path):
        html = _page(client, path).text
        for outside in ("cdn.tailwindcss.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com", "unpkg.com"):
            assert outside not in html, (path, outside)
        links = _STATIC_LINK.findall(html)
        assert {url for url, _ in links} >= {
            "/static/css/tailwind.css", "/static/fonts/fonts.css", "/static/vendor/chart.umd.min.js",
        }
        for url, _version in links:
            resp = client.get(url)
            assert resp.status_code == 200, url
            assert resp.content, url

    def test_static_needs_no_sign_in(self, client):
        assert client.get("/static/css/tailwind.css").status_code == 200

    def test_the_fonts_stylesheet_names_files_that_exist(self, client):
        css = client.get("/static/fonts/fonts.css").text
        files = re.findall(r"url\(\./([^)]+\.woff2)\)", css)
        assert len(files) >= 14
        for name in files:
            assert client.get(f"/static/fonts/{name}").status_code == 200, name

    def test_chart_js_is_pinned_and_asks_for_no_source_map(self):
        from app.core.static_assets import STATIC_DIR

        js = (STATIC_DIR / "vendor" / "chart.umd.min.js").read_text(encoding="utf-8")
        assert re.search(r"Chart\.js v4\.\d+\.\d+", js[:200])
        assert "sourceMappingURL" not in js


class TestVersionedUrls:
    def test_the_url_follows_the_content(self, tmp_path):
        from app.core import static_assets

        (tmp_path / "a.css").write_text("one")
        with patch.object(static_assets, "STATIC_DIR", tmp_path):
            first = static_assets.static_url("a.css")
            (tmp_path / "a.css").write_text("two, longer")
            second = static_assets.static_url("a.css")
        assert first.startswith("/static/a.css?v=") and second.startswith("/static/a.css?v=")
        assert first != second

    def test_a_missing_file_does_not_fail_the_page(self, tmp_path):
        from app.core import static_assets

        with patch.object(static_assets, "STATIC_DIR", tmp_path):
            assert static_assets.static_url("nope.css") == "/static/nope.css?v=missing"


class TestContentSecurityPolicy:
    @pytest.mark.parametrize("path", PAGES)
    def test_pages_carry_it(self, client, admin_user, path):
        csp = _page(client, path).headers.get("content-security-policy", "")
        assert "script-src 'self' 'unsafe-inline'" in csp
        assert "font-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "http" not in csp  # no outside host is allowed anything

    def test_api_responses_do_not(self, client, test_user):
        from tests.conftest import auth_header

        resp = client.get("/v1/me", headers=auth_header())
        assert resp.status_code == 200
        assert "content-security-policy" not in resp.headers

    def test_static_files_do_not(self, client):
        assert "content-security-policy" not in client.get("/static/css/tailwind.css").headers
