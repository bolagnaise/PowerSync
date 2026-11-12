"""Versioned comparison policy, kept outside the candidate component root."""
from .scoring import score
from pathlib import Path
import ast
import hashlib
from .trusted_contract import ContractError


def changed_component_paths(baseline, candidate):
    roots = [Path(path).resolve() for path in (baseline, candidate)]
    paths = set()
    for root in roots:
        paths.update(path.relative_to(root) for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    return ["custom_components/power_sync/" + path.as_posix() for path in sorted(paths)
            if any(not (root / path).is_file() for root in roots) or (roots[0] / path).read_bytes() != (roots[1] / path).read_bytes()]


def classify(bundle, left, right, *, tolerance=1e-6, terminal_price=0.0):
    if left["outcome"] != right["outcome"]:
        return {"outcome": "changed", "reason": "solver_path_changed"}
    if left.get("solver") != right.get("solver"):
        return {"outcome": "changed", "reason": "solver_backend_changed"}
    if left["outcome"] != "feasible":
        return {"outcome": "unsupported", "reason": "no_feasible_schedule"}
    a = score(bundle, left["schedule"], terminal_price=terminal_price)
    try:
        b = score(bundle, right["schedule"], terminal_price=terminal_price)
    except (ContractError, IndexError, KeyError, TypeError, ValueError, OverflowError):
        return {"outcome": "violation", "reason": "invalid_candidate_schedule", "baseline_score": a}
    result = {"baseline_score": a, "candidate_score": b}
    if a["violations"] or b["violations"]:
        return {**result, "outcome": "violation"}
    if a["unsupported_metrics"] or b["unsupported_metrics"]:
        return {**result, "outcome": "unsupported"}
    if left["schedule"] == right["schedule"]:
        return {**result, "outcome": "exact"}
    if any(any(a_row.get(key) != b_row.get(key) for key in ("action", "power_w", "grid_import_target_w")) or
           (a_row.get("soc") is None) != (b_row.get("soc") is None)
           for a_row, b_row in zip(left["schedule"], right["schedule"])):
        return {**result, "outcome": "unsupported", "reason": "unscored_command_fields"}
    equivalent = abs(a["common_score"] - b["common_score"]) <= tolerance and abs(a["terminal_energy_delta_kwh"] - b["terminal_energy_delta_kwh"]) <= 0.001
    return {**result, "outcome": "equivalent" if equivalent else "changed"}


def fidelity(bundle, left, right, *, terminal_price=0.0, exact_only=False):
    """Exact reconstruction/repeatability is distinct from metric support."""
    if all(left.get(key) == right.get(key) for key in ("outcome", "solver", "schedule", "exception")):
        return {"outcome": "exact"}
    if exact_only:
        return {"outcome": "changed", "reason": "captured_result_not_reproduced"}
    try:
        return classify(bundle, left, right, terminal_price=terminal_price)
    except (ContractError, IndexError, KeyError, TypeError, ValueError, OverflowError):
        return {"outcome": "invalid", "reason": "invalid_reference_schedule"}


def run_status(comparison, control, reconstruction, coverage, candidate_control=None):
    controls = [control, reconstruction] + ([candidate_control] if candidate_control is not None else [])
    if any(item["outcome"] not in ("exact", "equivalent") for item in controls):
        return "invalid"
    if comparison["outcome"] in ("changed", "violation"):
        return "fail"
    if comparison["outcome"] == "unsupported" or "not-covered" in coverage.values():
        return "insufficient"
    return "pass" if comparison["outcome"] in ("exact", "equivalent") else "invalid"


def reconstruction_surface(path):
    """Freeze everything except solver-reachable, non-constructor method bodies.

    Signatures/defaults/decorators, dataclasses, globals, properties, external
    methods and constructor/update_config helper graphs are always retained.
    New/unknown structural changes conservatively require a higher replay layer.
    """
    tree = ast.parse(Path(path).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BatteryOptimizer")
    methods = {node.name: node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    def called(node):
        # Include callbacks, class/static calls and literal getattr references,
        # not just self.method() syntax. Over-approximation is intentional.
        for item in ast.walk(node):
            if not (isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "getattr" and len(item.args) >= 2):
                continue
            target, name = item.args[:2]
            optimizer = (isinstance(target, ast.Name) and target.id in ("optimizer", "opt") or
                         isinstance(target, ast.Name) and target.id == "self" and any(node is method for method in methods.values()) or
                         isinstance(target, ast.Attribute) and target.attr in ("_optimizer", "optimizer"))
            if optimizer and not (isinstance(name, ast.Constant) and type(name.value) is str):
                return set(methods)  # Dynamic optimizer dispatch cannot prove a narrower surface.
        return {item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute) and item.attr in methods} | {
            item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and type(item.value) is str and item.value in methods}
    def reachable(roots):
        result, pending = set(), list(roots)
        while pending:
            name = pending.pop()
            if name in result or name not in methods:
                continue
            result.add(name)
            pending.extend(called(methods[name]))
        return result
    solving = reachable(["optimize"])
    properties = {name for name, node in methods.items() if any(
        isinstance(decorator, ast.Name) and decorator.id == "property" or
        isinstance(decorator, ast.Attribute) and decorator.attr in ("setter", "getter", "deleter")
        for decorator in node.decorator_list)}
    external = set()
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            external.update(called(node))
            external.update(item.id for item in ast.walk(node) if isinstance(item, ast.Name) and item.id in methods)
    for node in tree.body:
        if node is not cls:
            external.update(called(node))
    for other in Path(path).parents[1].rglob("*.py"):
        if other == Path(path) or "__pycache__" in other.parts:
            continue
        external.update(called(ast.parse(other.read_text())) - {"optimize"})
    implicit = {name for name in methods if name.startswith("__") and name.endswith("__")}
    frozen = reachable((set(methods) - solving) | properties | external | implicit | {"__init__", "update_config"})
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in solving - frozen:
            node.body = [ast.Pass()]
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def source_coverage(paths, baseline=None, candidate=None):
    """Resolved-state injection does not exercise construction or caller defaults."""
    supported = "custom_components/power_sync/optimization/battery_optimizer.py"
    if supported not in paths:
        return {path: "not-covered" for path in paths}
    surface_matches = False
    if baseline is not None and candidate is not None:
        try:
            surface_matches = reconstruction_surface(Path(baseline) / "optimization/battery_optimizer.py") == reconstruction_surface(Path(candidate) / "optimization/battery_optimizer.py")
        except (OSError, SyntaxError, StopIteration, ValueError):
            pass
    return {path: "solver_v1" if path == supported and surface_matches else "not-covered" for path in paths}
