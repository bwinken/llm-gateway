"""Which service module may import which.

**Backends.** The usage seam, the SSE pump and the timeouts used to sit in
``vllm_proxy.py``, so the Azure and Bedrock proxies imported them from the
vLLM module. A cloud proxy takes shared code from ``billing.py`` and
``downstream.py``, and those two know no backend.

**Client rules.** ``client_patch`` (a patch's text and scripts) knows no check
and no database; ``client_rules`` (checks, violations, notices) builds on it;
``client_mail`` (digest, queue, mail log) builds on both. Never the other way
round, and client setup (``client_settings``) reads neither rules nor mail.
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
