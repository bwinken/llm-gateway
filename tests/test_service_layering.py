"""Which service module may import which.

**Backends.** The usage seam, the SSE pump and the timeouts used to sit in
``vllm_proxy.py``, so the Azure and Bedrock proxies imported them from the
vLLM module. A cloud proxy takes shared code from ``billing.py`` and
``downstream.py``, and those two know no backend.

**Client rules.** ``client_patch`` (a patch's text and scripts) knows no check
and no database; ``client_rules`` (checks, violations, notices) builds on it;
``client_mail`` (digest, queue, mail log) builds on both. Never the other way
round, and client setup (``client_settings``) reads neither rules nor mail.

**Model changes.** ``model_changes`` (who a replaced model is announced to)
builds on ``client_patch`` and ``client_rules``; ``client_mail`` sends its
mail. The rules know nothing of it, and it never sends by itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
PROXIES = {"vllm_proxy", "azure_proxy", "bedrock_proxy"}


def _service_imports(module: str) -> set[str]:
    """Names of the ``app.services`` modules ``module`` imports, anywhere in it."""
    found: set[str] = set()
    tree = ast.parse((SERVICES / f"{module}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app.services":
                found.update(alias.name for alias in node.names)
            elif node.module.startswith("app.services."):
                found.add(node.module.split(".")[2])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.services."):
                    found.add(alias.name.split(".")[2])
    return found


@pytest.mark.parametrize("proxy", ["azure_proxy", "bedrock_proxy"])
def test_a_cloud_proxy_takes_nothing_from_the_vllm_proxy(proxy):
    imports = _service_imports(proxy)
    assert {"billing", "downstream"} <= imports  # the walk does find imports
    assert "vllm_proxy" not in imports


@pytest.mark.parametrize("shared", ["billing", "downstream"])
def test_the_shared_modules_know_no_backend(shared):
    imports = _service_imports(shared)
    assert "observability" in imports  # the walk does find imports
    assert not imports & PROXIES


def test_every_backend_bills_through_the_one_seam():
    from app.services import azure_proxy, bedrock_proxy, billing, vllm_proxy

    for proxy in (vllm_proxy, azure_proxy, bedrock_proxy):
        assert proxy._log_usage is billing._log_usage
        assert proxy._log_error is billing._log_error


CLIENT_MODULES = {"client_patch", "client_rules", "client_mail"}


def test_the_client_modules_depend_one_way():
    patch, rules, mail = (_service_imports(name) for name in ("client_patch", "client_rules", "client_mail"))
    assert not patch & CLIENT_MODULES
    assert rules & CLIENT_MODULES == {"client_patch"}
    assert mail & CLIENT_MODULES == {"client_patch", "client_rules"}


def test_model_changes_sit_between_the_rules_and_the_mail():
    changes = _service_imports("model_changes")
    assert {"client_patch", "client_rules"} <= changes         # the walk does find imports
    assert not changes & {"client_mail", "mailer"}             # every send path is client_mail.send_due
    assert "model_changes" in _service_imports("client_mail")
    assert "model_changes" not in _service_imports("client_rules") | _service_imports("client_patch")


def test_the_patch_module_reads_no_database():
    tree = ast.parse((SERVICES / "client_patch.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert "json" in imported  # the walk does find imports
    # `app.core.config` (the site's title, printed by the scripts) is all it takes from the app.
    assert not {
        name for name in imported
        if name.startswith(("sqlmodel", "sqlalchemy", "app.core.database", "app.models", "app.services"))
    }


def test_client_setup_reads_neither_the_rules_nor_their_mail():
    imports = _service_imports("client_settings")
    assert "client_patch" in imports  # the variables that hold the key
    assert not imports & {"client_rules", "client_mail", "client_checks"}


# ── The account's answers: one service, two routers ──

ROUTERS = SERVICES.parent / "routers"
ACCOUNT_ANSWERS = ("me", "status", "usage", "service_account_row", "notices")


def _imports(path: Path) -> set[str]:
    """Every module ``path`` imports, as dotted names (``from a.b import c``
    gives ``a.b`` and ``a.b.c``)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_account_answers_know_no_router_and_no_request():
    """``account_view`` builds what ``/v1/me`` ... ``/v1/notices`` and
    ``/dashboard/api/*`` answer. It takes the user a route authenticated and
    returns the body: no request, no response, no router."""
    imports = _imports(SERVICES / "account_view.py")
    assert "app.core.deps" in imports  # the walk does find imports
    assert not [name for name in imports if name.startswith(("fastapi", "starlette", "app.routers"))]


def test_both_routers_call_the_service_and_neither_calls_the_other():
    v1 = ROUTERS / "v1_api.py"
    dashboard = sorted((ROUTERS / "dashboard").glob("*.py"))
    assert len(dashboard) >= 4
    assert not [name for name in _imports(v1) if name.startswith("app.routers")]
    for module in dashboard:
        assert "app.routers.v1_api" not in _imports(module), module.name
    api = (ROUTERS / "dashboard" / "api.py").read_text(encoding="utf-8")
    by_key = v1.read_text(encoding="utf-8")
    for name in ACCOUNT_ANSWERS:
        assert f"account_view.{name}" in api, name
    # By key the row is reached through the whole list.
    for name in ("me", "status", "usage", "service_accounts", "notices"):
        assert f"account_view.{name}" in by_key, name
