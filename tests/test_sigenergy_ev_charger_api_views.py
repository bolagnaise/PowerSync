"""Regression tests for Sigenergy EV charger API view wiring."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

# Views that reach Sigenergy helpers while serving a request.  These run
# outside any scope that binds a request-local ``hass``, so they must use the
# reference stored on the view at construction time.
SIGENERGY_VIEWS = ("EVVehiclesView", "EVLoadpointStatusView")

SIGENERGY_HASS_HELPERS = (
    "_configured_sigenergy_charger_capabilities",
    "_read_sigenergy_charger_state_for_entry",
)


def _view_class(tree: ast.AST, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{class_name} not found")


def _sigenergy_helper_hass_args(node: ast.AST) -> list[ast.AST]:
    """Collect the ``hass`` argument of every Sigenergy helper call under node."""
    args: list[ast.AST] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in SIGENERGY_HASS_HELPERS
        ):
            assert len(child.args) >= 2, ast.unparse(child)
            args.append(child.args[1])
    return args


def _is_self_hass(arg: ast.AST) -> bool:
    return (
        isinstance(arg, ast.Attribute)
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "self"
        and arg.attr == "_hass"
    )


def test_sigenergy_ev_api_views_use_stored_hass_reference():
    """Sigenergy helpers must be handed ``self._hass``, never a bare ``hass``.

    Scans the whole class rather than only ``get`` — the calls have moved
    between methods before, which silently retired this guard.
    """
    tree = ast.parse(INIT_PATH.read_text())

    for class_name in SIGENERGY_VIEWS:
        hass_args = _sigenergy_helper_hass_args(_view_class(tree, class_name))

        assert hass_args, f"{class_name} makes no Sigenergy helper call"
        assert all(_is_self_hass(arg) for arg in hass_args), [
            ast.unparse(arg) for arg in hass_args if not _is_self_hass(arg)
        ]


def test_no_api_view_method_reads_an_unbound_hass():
    """``hass`` is not a module global, so a bare read is a NameError at runtime.

    The Sigenergy branch of ``EVVehiclesView.get`` regressed exactly this way
    and 500'd the whole vehicle list for anyone with a charger configured.
    """
    tree = ast.parse(INIT_PATH.read_text())

    assert not [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "hass" for t in node.targets
        )
    ], "a module-level `hass` would invalidate this guard"

    offenders: list[str] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for method in cls.body:
            if not isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue

            bound = {a.arg for a in method.args.args}
            bound |= {a.arg for a in method.args.kwonlyargs}
            for child in ast.walk(method):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    bound.add(child.id)
                elif isinstance(child, ast.arg):
                    bound.add(child.arg)
                elif isinstance(child, (ast.Import, ast.ImportFrom)):
                    for alias in child.names:
                        bound.add((alias.asname or alias.name).split(".")[0])
            if "hass" in bound:
                continue

            offenders += [
                f"{cls.name}.{method.name}() line {child.lineno}"
                for child in ast.walk(method)
                if isinstance(child, ast.Name)
                and child.id == "hass"
                and isinstance(child.ctx, ast.Load)
            ]

    assert not offenders, offenders
