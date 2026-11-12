"""Load one explicitly selected component into an isolated worker process."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import importlib
import platform
from pathlib import Path
import sys
import types


def install_stubs(component_root: Path, decision_time: datetime):
    """Shared benchmark/replay loader; never start Home Assistant."""
    component_root = component_root.resolve(strict=True)
    for name in tuple(sys.modules):
        if name == "power_sync" or name.startswith("power_sync.") or name == "homeassistant" or name.startswith("homeassistant."):
            del sys.modules[name]
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda *a, **kw: decision_time
    dt.utcnow = lambda *a, **kw: decision_time.astimezone(timezone.utc)
    dt.UTC = timezone.utc
    util.dt, ha.util = dt, util
    sys.modules.update({"homeassistant": ha, "homeassistant.util": util, "homeassistant.util.dt": dt})
    for name, directory in (("power_sync", component_root), ("power_sync.optimization", component_root / "optimization")):
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package


def load_component(component_root: Path, decision_time: datetime):
    """Worker-only loader. No Home Assistant startup, provider or controller."""
    install_stubs(component_root, decision_time)
    return importlib.import_module("power_sync.optimization.battery_optimizer")


def source_binding(component_root: Path, module):
    """Explicit off-path source/environment identity, not remote attestation."""
    root = component_root.resolve()
    tree = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"):
        tree.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    requirements = root.parents[1] / "requirements-test.txt"
    return {"component_sha256": tree.hexdigest(),
            "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
            "python": platform.python_version(), "platform": platform.machine(),
            "solver": "highs" if module.HIGHS_AVAILABLE else "greedy",
            "solver_version": module.highspy.Highs().version() if module.HIGHS_AVAILABLE else "unavailable"}


def dependency_inventory(component_root: Path):
    """Conservative source tripwire: new attribute dependencies need review."""
    tree = ast.parse((component_root / "optimization/battery_optimizer.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BatteryOptimizer")
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    attributes = {n.attr for n in ast.walk(cls) if isinstance(n, ast.Attribute)
                  and isinstance(n.value, ast.Name) and n.value.id == "self" and n.attr not in methods}
    attributes |= {n.args[1].value for n in ast.walk(cls) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "getattr" and len(n.args) > 1
                   and isinstance(n.args[0], ast.Name) and n.args[0].id == "self" and isinstance(n.args[1], ast.Constant)}
    return attributes - methods


def replay(bundle, component_root, *, allow_changed_source=False):
    """Called only inside the subprocess. The caller owns the trusted scorer."""
    from . import trusted_contract as contract
    contract.validate_bundle(bundle)
    root = Path(component_root).resolve(strict=True)
    clock = datetime.fromisoformat(bundle["decision_time"])
    if clock.tzinfo is None:
        raise contract.ContractError("naive_timestamp")
    module = load_component(root, clock)
    if dependency_inventory(root) != set(contract.STATE_FIELDS):
        raise contract.ContractError("unsupported_state_dependencies")
    import inspect
    if tuple(inspect.signature(module.BatteryOptimizer.optimize).parameters)[1:] != contract.INPUT_FIELDS:
        raise contract.ContractError("unsupported_signature")
    binding = source_binding(root, module)
    if not allow_changed_source and binding != bundle["source"]:
        raise contract.ContractError("source_environment_mismatch")
    if any(binding[key] != bundle["source"][key] for key in binding if key != "component_sha256"):
        raise contract.ContractError("dependency_image_mismatch")
    optimizer = module.BatteryOptimizer()
    for key, value in bundle["state"].items():
        setattr(optimizer, key, contract.decode(value, module))
    inputs = {key: contract.decode(value, module) for key, value in bundle["inputs"].items()}
    import time
    import resource
    started = time.monotonic()
    try:
        result = optimizer.optimize(**inputs)
    except Exception as error:
        return {"outcome": "exception", "exception": type(error).__name__ if type(error).__name__ in ("ValueError", "RuntimeError", "TimeoutError") else "other",
                "source": binding, "schedule": [], "elapsed_seconds": time.monotonic() - started}
    codec = contract.Codec()
    return {"outcome": "feasible" if result.feasible else "infeasible", "source": binding,
            "schedule": contract.schedule_snapshot(result.schedule, codec), "solver": result.solver_used,
            "elapsed_seconds": time.monotonic() - started,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
