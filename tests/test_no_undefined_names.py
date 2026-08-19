"""Guard against ``NameError`` bugs that only surface on a live install.

Four of these shipped at once: two EV API-view calls that passed a bare ``hass``
that was never bound, two constants used but missing from the ``.const`` import
block, and the optimizer's greedy fallback calling itself with the *callee's*
parameter names instead of its own.  Every one of them is invisible to import
smoke tests -- the module imports fine and only raises when that specific branch
runs, which for the optimizer fallback means the moment the LP gives up.

The scan is flow-insensitive on purpose: a name is only reported when it is
bound in *no* enclosing scope, so conditional binding never produces a false
alarm.  Anything this test reports is a guaranteed ``NameError`` if the line
executes.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_BUILTINS = set(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__builtins__",
    "__debug__",
    "WindowsError",
}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _store_targets(node: ast.AST) -> set[str]:
    return {
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
    }


def _bound_names(scope: ast.AST) -> set[str]:
    """Names bound in ``scope``'s own body, ignoring nested scopes."""
    out: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(child.name)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, _COMPREHENSIONS):
                # Only a walrus inside a comprehension leaks outward.
                for inner in ast.walk(child):
                    if isinstance(inner, ast.NamedExpr):
                        out.update(_store_targets(inner.target))
                continue
            if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For,
                                  ast.AsyncFor, ast.NamedExpr)):
                for target in getattr(child, "targets", None) or [child.target]:
                    out.update(_store_targets(target))
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    out.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        out.update(_store_targets(item.optional_vars))
            elif isinstance(child, ast.ExceptHandler) and child.name:
                out.add(child.name)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                out.update(child.names)
            elif isinstance(child, ast.Match):
                for inner in ast.walk(child):
                    name = getattr(inner, "name", None) or getattr(inner, "rest", None)
                    if isinstance(inner, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)) and name:
                        out.add(name)
            walk(child)

    walk(scope)
    return out


def _parameters(fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    args = fn.args
    out = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        out.add(args.vararg.arg)
    if args.kwarg:
        out.add(args.kwarg.arg)
    return out


def _own_nodes(node: ast.AST):
    """Yield ``node`` and its descendants, stopping at nested scopes.

    When ``node`` is itself a scope, only the parts evaluated in the *enclosing*
    scope are yielded: decorators, base classes, defaults and annotations.
    """
    if isinstance(node, _SCOPES):
        roots = list(getattr(node, "decorator_list", []))
        roots += list(getattr(node, "bases", []))
        roots += [kw.value for kw in getattr(node, "keywords", [])]
        args = getattr(node, "args", None)
        if args is not None and not isinstance(args, list):
            roots += [d for d in (*args.defaults, *args.kw_defaults) if d is not None]
        if getattr(node, "returns", None) is not None:
            roots.append(node.returns)
        for root in roots:
            yield from _own_nodes(root)
        return

    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _SCOPES + _COMPREHENSIONS):
                continue
            stack.append(child)


def _child_scopes(node: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPES):
            out.append(child)
        else:
            out.extend(_child_scopes(child))
    return out


def undefined_names(source: str, filename: str) -> list[tuple[int, str]]:
    tree = ast.parse(source, filename)
    found: list[tuple[int, str]] = []

    def check(node: ast.AST, chain: list[set[str]]) -> None:
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in _BUILTINS
            and not any(node.id in scope for scope in chain)
        ):
            found.append((node.lineno, node.id))

    def visit(scope: ast.AST, chain: list[set[str]]) -> None:
        if isinstance(scope, ast.ClassDef):
            # A class body is visible to itself but not to nested functions.
            body_chain = chain + [_bound_names(scope)]
            for stmt in scope.body:
                for node in _own_nodes(stmt):
                    check(node, body_chain)
            for child in _child_scopes(scope):
                visit(child, chain)
            return

        names = _parameters(scope)
        if not isinstance(scope, ast.Lambda):
            names |= _bound_names(scope)
        inner = chain + [names]
        body = scope.body if not isinstance(scope, ast.Lambda) else [scope.body]
        for stmt in body:
            for node in _own_nodes(stmt):
                check(node, inner)
        for child in _child_scopes(scope):
            visit(child, inner)

    module_chain = [_bound_names(tree)]
    for stmt in tree.body:
        for node in _own_nodes(stmt):
            check(node, module_chain)
    for child in _child_scopes(tree):
        visit(child, module_chain)

    return sorted(set(found))


def test_scanner_detects_a_known_unbound_name():
    """Keep the scanner honest -- a silent no-op would guard nothing."""
    source = (
        "class View:\n"
        "    async def get(self):\n"
        "        return helper(entry, hass)\n"
        "\n"
        "def helper(a, b):\n"
        "    return a\n"
    )
    assert undefined_names(source, "<probe>") == [(3, "entry"), (3, "hass")]


def test_scanner_accepts_names_bound_in_an_enclosing_scope():
    source = (
        "import os\n"
        "\n"
        "class View:\n"
        "    def __init__(self, hass):\n"
        "        self._hass = hass\n"
        "\n"
        "    def get(self):\n"
        "        [x for x in range(3)]\n"
        "        with open(os.devnull) as fh:\n"
        "            return fh, self._hass\n"
    )
    assert undefined_names(source, "<probe>") == []


def test_component_has_no_undefined_names():
    offenders: list[str] = []
    for path in sorted(COMPONENT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, name in undefined_names(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"{path.relative_to(COMPONENT)}:{lineno}: undefined name {name!r}")

    assert not offenders, "guaranteed NameError at runtime:\n" + "\n".join(offenders)
