"""Versioned comparison policy, kept outside the candidate component root."""
from .scoring import score
from pathlib import Path


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
    a, b = (score(bundle, r["schedule"], terminal_price=terminal_price) for r in (left, right))
    result = {"baseline_score": a, "candidate_score": b}
    if a["violations"] or b["violations"]:
        return {**result, "outcome": "violation"}
    if a["unsupported_metrics"] or b["unsupported_metrics"]:
        return {**result, "outcome": "unsupported"}
    if left["schedule"] == right["schedule"]:
        return {**result, "outcome": "exact"}
    equivalent = abs(a["common_score"] - b["common_score"]) <= tolerance and abs(a["terminal_energy_delta_kwh"] - b["terminal_energy_delta_kwh"]) <= 0.001
    return {**result, "outcome": "equivalent" if equivalent else "changed"}


def source_coverage(paths):
    """Conservative path report; a solver pass never covers coordinator edits."""
    supported = {
        "custom_components/power_sync/optimization/battery_optimizer.py",
        "custom_components/power_sync/optimization/cost_neutral.py",
        "custom_components/power_sync/optimization/ev_load_plan.py",
    }
    return {path: "solver_v1" if path in supported else "not-covered" for path in paths}
