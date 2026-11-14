"""What the backends share lives in ``billing.py`` and ``downstream.py``.

The usage seam, the SSE pump and the timeouts used to sit in ``vllm_proxy.py``,
so the Azure and Bedrock proxies imported them from the vLLM module. These
tests keep the split: a cloud proxy takes shared code from the two shared
modules, and those two know no backend.
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
