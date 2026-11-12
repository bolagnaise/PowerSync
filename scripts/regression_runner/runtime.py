"""Load one explicitly selected component into an isolated worker process."""
from __future__ import annotations

import ast
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import importlib
from importlib import metadata
import json
import platform
from pathlib import Path
import re
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
    dt.now = lambda time_zone=None: decision_time.astimezone(time_zone or decision_time.tzinfo)
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


def installed_dependencies():
    from .trusted_contract import ContractError
    installed = {}
    for distribution in metadata.distributions():
        name, version = distribution.metadata.get("Name"), distribution.version
        if type(name) is not str or type(version) is not str or not name or not version:
            raise ContractError("dependency_image_unavailable")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in installed:
            raise ContractError("ambiguous_dependency_image")
        installed[normalized] = version
    return installed


def verify_dependencies(requirements: bytes, installed=None):
    """A lock-file digest identifies an image only when its pins are installed."""
    from .trusted_contract import ContractError
    expected_versions = {}
    try:
        lines = requirements.decode("utf-8").splitlines()
    except UnicodeError:
        raise ContractError("unsupported_dependency_contract") from None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if match is None:
            raise ContractError("unsupported_dependency_contract")
        name, expected = match.groups()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in expected_versions:
            raise ContractError("unsupported_dependency_contract")
        expected_versions[normalized] = expected
    if not expected_versions:
        raise ContractError("unsupported_dependency_contract")
    installed = installed_dependencies() if installed is None else installed
    for name, expected in expected_versions.items():
        if name not in installed:
            raise ContractError("dependency_image_unavailable")
        if installed[name] != expected:
            raise ContractError("dependency_image_mismatch")
    return installed


def component_digest(component_root):
    from importlib.machinery import EXTENSION_SUFFIXES
    root = Path(component_root).resolve(strict=True)
    tree = hashlib.sha256()
    total = count = 0
    from .trusted_contract import ContractError
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ContractError("unsupported_source_symlink")
        if path.suffix == ".pyc":
            raise ContractError("unsupported_sourceless_bytecode")
        if any(path.name.endswith(suffix) for suffix in EXTENSION_SUFFIXES):
            raise ContractError("unsupported_native_component")
        if not path.is_file():
            continue
        count += 1;total += path.stat().st_size
        if count>20000 or path.stat().st_size>8*1024*1024 or total>64*1024*1024:
            raise ContractError("source_budget_exceeded")
        name, content = path.relative_to(root).as_posix().encode(), path.read_bytes()
        tree.update(len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content)
    return tree.hexdigest()


def source_binding(component_root: Path, module):
    """Explicit off-path source/environment identity, not remote attestation."""
    root = component_root.resolve()
    requirements = (root.parents[1] / "requirements-test.txt").read_bytes()
    installed = verify_dependencies(requirements)
    return {"component_sha256": component_digest(root),
            "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
            "installed_dependencies_sha256": hashlib.sha256(json.dumps(installed, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "python": platform.python_version(), "platform": f"{platform.system()}_{platform.machine()}",
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
    clock = contract.decision_clock(bundle)
    module = load_component(root, clock)
    if dependency_inventory(root) != set(contract.STATE_FIELDS):
        raise contract.ContractError("unsupported_state_dependencies")
    import inspect
    if tuple(inspect.signature(module.BatteryOptimizer.optimize).parameters)[1:] != contract.INPUT_FIELDS:
        raise contract.ContractError("unsupported_signature")
    for name, expected in contract.CLASS_FIELDS.items():
        if tuple(field.name for field in fields(getattr(module, name))) != expected:
            raise contract.ContractError("unsupported_dataclass")
    from power_sync.regression.environment import POLICY_SHA, binding as ha_binding
    binding = ha_binding(root, module) if bundle["source"]["requirements_sha256"] == POLICY_SHA else source_binding(root, module)
    if not allow_changed_source and binding != bundle["source"]:
        raise contract.ContractError("source_environment_mismatch")
    if any(binding[key] != bundle["source"][key] for key in binding if key != "component_sha256"):
        raise contract.ContractError("dependency_image_mismatch")
    optimizer = module.BatteryOptimizer()
    for key, value in bundle["state"].items():
        setattr(optimizer, key, contract.decode(value, module))
    inputs = {key: contract.decode(value, module) for key, value in bundle["inputs"].items()}
    before_codec = contract.Codec()
    before = contract.state_snapshot(optimizer, before_codec)
    import time
    import resource
    started = time.monotonic()
    try:
        result = optimizer.optimize(**inputs)
    except Exception as error:
        return {"outcome": "exception", "exception": type(error).__name__ if type(error).__name__ in ("ValueError", "RuntimeError", "TimeoutError", "CancelledError", "KeyboardInterrupt") else "other",
                "source": binding, "schedule": [], "solver": None, "elapsed_seconds": time.monotonic() - started}
    codec = contract.Codec()
    check = contract.Codec()
    check.aliases = dict(before_codec.aliases)
    after = contract.state_snapshot(optimizer, check)
    if any(after.get(key) != value for key, value in before.items() if key not in contract.SOLVE_MUTATIONS):
        raise contract.ContractError("unsupported_state_mutation")
    return {"outcome": "feasible" if result.feasible else "infeasible", "source": binding,
            "schedule": contract.schedule_snapshot(result.schedule, codec), "solver": result.solver_used if result.solver_used in ("highs", "greedy") else None,
            "elapsed_seconds": time.monotonic() - started,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
