"""HA solver runtime identity. Does not require the developer test environment."""
from importlib import metadata
import hashlib
import json
from pathlib import Path
import platform

POLICY = {"profile": "ha-solver-runtime-v1", "distributions": ["highspy", "numpy"], "source": "component-tree-v2"}
POLICY_SHA = hashlib.sha256(json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def component_digest(root):
    tree = hashlib.sha256()
    total = count = 0
    for path in sorted(Path(root).rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError("source_symlink")
        if path.is_file() and path.suffix == ".pyc":
            raise ValueError("source_bytecode")
        if path.is_file():
            count += 1;total += path.stat().st_size
            if count>20000 or path.stat().st_size>8*1024*1024 or total>64*1024*1024:
                raise ValueError("source_budget_exceeded")
            name, content = path.relative_to(root).as_posix().encode(), path.read_bytes()
            tree.update(len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content)
    return tree.hexdigest()


def binding(root, module):
    versions = {}
    for name in POLICY["distributions"]:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return {"component_sha256": component_digest(root), "requirements_sha256": POLICY_SHA,
        "installed_dependencies_sha256": hashlib.sha256(json.dumps(versions, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "python": platform.python_version(), "platform": f"{platform.system()}_{platform.machine()}",
        "solver": "highs" if module.HIGHS_AVAILABLE else "greedy",
        "solver_version": module.highspy.Highs().version() if module.HIGHS_AVAILABLE else "unavailable"}
