"""Regression tests for normalized EV loadpoint endpoint wiring."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _get_method() -> ast.AsyncFunctionDef:
    tree = ast.parse(INIT_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EVLoadpointStatusView":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "get":
                    return child
    raise AssertionError("EVLoadpointStatusView.get not found")


def test_loadpoint_site_surplus_uses_normalized_total_ev_power():
    method = _get_method()
    calls = [
        node
        for node in ast.walk(method)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_calculate_solar_surplus"
        )
    ]

    assert len(calls) == 1
    assert len(calls[0].args) >= 2
    assert isinstance(calls[0].args[1], ast.Name)
    assert calls[0].args[1].id == "total_ev_power_kw"
    assert any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "preliminary_loadpoints"
            for target in node.targets
        )
        and node.lineno < calls[0].lineno
        for node in ast.walk(method)
    )


def test_hacs_ocpp_discovery_is_enabled_and_claim_filtered():
    method = _get_method()
    source = ast.unparse(method)

    assert "if opts.get(CONF_OCPP_ENABLED) else ()" in source
    assert "claimed_hacs_ocpp_prefixes" in source
    assert "prefix in claimed_prefixes" in source
