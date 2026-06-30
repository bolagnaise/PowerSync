"""Regression tests for Sigenergy EV charger API view wiring."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _class_method(tree: ast.AST, class_name: str, method_name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == method_name:
                    return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _sigenergy_capability_hass_args(method: ast.AsyncFunctionDef) -> list[ast.AST]:
    args: list[ast.AST] = []
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_configured_sigenergy_charger_capabilities"
        ):
            assert len(node.args) >= 2
            args.append(node.args[1])
    return args


def test_sigenergy_ev_api_views_use_stored_hass_reference_for_capabilities():
    source = INIT_PATH.read_text()
    tree = ast.parse(source)

    for class_name in ("EVWidgetDataView", "EVLoadpointStatusView"):
        method = _class_method(tree, class_name, "get")
        hass_args = _sigenergy_capability_hass_args(method)

        assert hass_args
        assert all(
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "self"
            and arg.attr == "_hass"
            for arg in hass_args
        )
