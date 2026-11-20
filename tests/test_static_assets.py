"""The pages load their stylesheet, Chart.js and fonts from this site only
(app/static, app/core/static_assets.py), under URLs that change with the
content, and say so to the browser with a Content-Security-Policy."""

from __future__ import annotations

import re
from pathlib import Path
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


# ── A class a page uses has a rule on that page ──
#
# The pages' own classes (.um-row, .ud-head, .op-input, ...) are written in
# <style> blocks, not built by Tailwind, so nothing checked them: a commit that
# took one feature out of admin.html removed 85 lines of rules with it, and the
# account drawer, the owner picker, the row menu and the Special access chips
# were served unstyled while every test passed.

_TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
_STYLESHEET = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "tailwind.css"

# What each page is made of: its own file, base.html, and the partials rendered
# into it. Their <style> blocks, with the built stylesheet, are all it loads.
_PAGE_PARTS = {
    "admin.html": ["_admin_tabs.html", "_admin_user_table.html", "_admin_user_detail.html", "_admin_macros.html"],
    "admin_models.html": ["_admin_tabs.html"],
    "admin_guide.html": ["_admin_tabs.html"],
    "dashboard.html": ["_client_notices.html", "_model_changes.html"],
    "setup.html": [],
    "welcome.html": [],
    "disabled.html": [],
}
# The prefixes of the classes the pages define themselves.
_OWN_CLASS = re.compile(r"^(?:(?:ag|gr|gw|mc|op|sa|ud|um)-[a-z][\w-]*[a-z0-9]|op)$")
# Classes that are only looked up by a script and have no look of their own.
_SCRIPT_HOOKS = {"gw-attention", "gw-tip-wide", "gw-checkbox", "mc-test-result", "um-disabled-badge"}

_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
_CLASS_ATTR = re.compile(r"""class(?:Name)?\s*=\s*(["'])(.*?)\1""", re.S)
_CLASS_LIST = re.compile(r"classList\.(?:add|remove|toggle)\(\s*'([\w-]+)'")
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def _rules(css: str) -> set[str]:
    return set(re.findall(r"\.(-?[A-Za-z_][\w-]*)", css))


def _classes_without_a_rule(page: str) -> dict[str, list[str]]:
    parts = ["base.html", page, *_PAGE_PARTS[page]]
    text = {name: (_TEMPLATES / name).read_text(encoding="utf-8") for name in parts}
    defined = _rules(_STYLESHEET.read_text(encoding="utf-8"))
    for source in text.values():
        defined |= _rules("".join(_STYLE_BLOCK.findall(source)))
    missing: dict[str, list[str]] = {}
    for name, source in text.items():
        body = _STYLE_BLOCK.sub("", source)
        used = set(_CLASS_LIST.findall(body))
        for match in _CLASS_ATTR.finditer(body):
            used.update(_JINJA.sub(" ", match.group(2)).split())
        for cls in used:
            if _OWN_CLASS.match(cls) and cls not in defined and cls not in _SCRIPT_HOOKS:
                missing.setdefault(cls, []).append(name)
    return missing


class TestEveryClassHasARule:
    @pytest.mark.parametrize("page", sorted(_PAGE_PARTS))
    def test_a_class_the_page_uses_is_styled_on_that_page(self, page):
        assert _classes_without_a_rule(page) == {}, (
            "used but no rule in this page's <style> blocks, base.html or the stylesheet"
        )

    def test_every_partial_belongs_to_a_page(self):
        # A new partial is added to _PAGE_PARTS, or its classes go unchecked.
        listed = {"base.html", *_PAGE_PARTS} | {part for parts in _PAGE_PARTS.values() for part in parts}
        assert {p.name for p in _TEMPLATES.glob("*.html")} == listed

    def test_the_check_sees_what_was_lost(self):
        # The rules that went missing, by name: the check must really read them.
        assert _OWN_CLASS.match("op") and _OWN_CLASS.match("um-menu") and not _OWN_CLASS.match("text-t3")
        admin = "".join(_STYLE_BLOCK.findall((_TEMPLATES / "admin.html").read_text(encoding="utf-8")))
        for cls in ("sa-chip", "op-input", "um-menu", "ud-head", "gr-rule", "gw-att-chip"):
            assert cls in _rules(admin), cls
        assert "#udDrawer" in admin and "#opMenu" in admin
