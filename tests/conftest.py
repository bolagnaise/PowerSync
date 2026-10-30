"""Shared pytest hygiene for PowerSync unit tests."""

from __future__ import annotations

import contextlib
import sys

import pytest


_MISSING = object()


@contextlib.contextmanager
def pin_power_sync_clock(*modules, **clock):
    """Pin dt helpers on every live ``power_sync`` ``dt_util`` binding.

    Several test modules install their own ``homeassistant.util.dt`` stub at
    import time, so whichever module pytest collects last wins and the rest
    silently inherit a foreign clock -- a frozen 2026-07-08 in one file, a live
    wall clock offset by +10h in another.  Pinning per test, against the module
    objects the product code actually holds, makes each file's clock
    independent of collection order.

    ``modules`` names extra modules to pin explicitly.  A test file that holds
    its own reference to a product module needs this: a later file can swap the
    ``sys.modules`` entry, leaving that reference live but unreachable from the
    scan below.
    """
    targets = []
    seen = set()
    candidates = list(modules)
    candidates += [m for n, m in list(sys.modules.items()) if n.startswith("power_sync")]
    for module in candidates:
        dt_module = getattr(module, "dt_util", None)
        if dt_module is None or id(dt_module) in seen:
            continue
        seen.add(id(dt_module))
        targets.append(dt_module)

    saved = [
        (target, {key: getattr(target, key, _MISSING) for key in clock})
        for target in targets
    ]
    for target in targets:
        for key, value in clock.items():
            setattr(target, key, value)
    try:
        yield
    finally:
        for target, previous in saved:
            for key, value in previous.items():
                if value is _MISSING:
                    delattr(target, key)
                else:
                    setattr(target, key, value)


@contextlib.contextmanager
def pinned_sys_modules(mapping):
    """Reinstate this test module's stub tree for the duration of one test.

    Product code imports some helpers lazily inside a function (the recorder,
    for one), so it resolves ``sys.modules`` at call time and picks up whatever
    stub tree was installed last -- not the one its own test file built.
    """
    saved = {name: sys.modules.get(name, _MISSING) for name in mapping}
    sys.modules.update(mapping)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@pytest.fixture(autouse=True)
def restore_real_power_sync_const():
    """Prevent module-level const stubs leaking into unrelated tests."""
    const_module = sys.modules.get("power_sync.const")
    if const_module is not None and not getattr(const_module, "__file__", None):
        sys.modules.pop("power_sync.const", None)

    aiohttp_module = sys.modules.get("aiohttp")
    if (
        aiohttp_module is not None
        and not getattr(aiohttp_module, "__file__", None)
        and not hasattr(aiohttp_module, "ClientSession")
    ):
        sys.modules.pop("aiohttp", None)

    yield


@pytest.fixture(autouse=True)
def complete_ha_storage_stub(monkeypatch):
    """Give lightweight HA Store fakes the async persistence API used by EV fences."""
    storage = sys.modules.get("homeassistant.helpers.storage")
    store_type = getattr(storage, "Store", None)
    if store_type is None or hasattr(store_type, "async_load"):
        return

    async def load(store):
        return getattr(store, "_test_value", None)

    async def save(store, value):
        store._test_value = value

    monkeypatch.setattr(store_type, "async_load", load, raising=False)
    monkeypatch.setattr(store_type, "async_save", save, raising=False)


def load_power_sync_function(name, namespace, path=None):
    """Exec one real ``__init__.py`` function (by AST) into ``namespace``."""
    import ast
    import pathlib

    path = path or pathlib.Path(__file__).resolve().parents[1] / "custom_components/power_sync/__init__.py"
    source = path.read_text()
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec",
                 flags=__import__("__future__").annotations.compiler_flag), namespace)
    return namespace[name]


def seed_powerwall_store_closures(namespace, *, hass, entry, domain="power_sync"):
    """Seed the setup-scope names Powerwall persistence closures resolve at call time.

    Since v2.12.15xx Tesla store writes go through ``_update_powerwall_store``
    guarded by ``_command_generation`` and ``_local_powerwall_generation_current``.
    Missing names are swallowed by handler ``except Exception`` blocks, so seed
    the real helpers rather than letting a probe silently no-op.
    """
    import asyncio

    namespace.setdefault("asyncio", asyncio)
    namespace.setdefault("DOMAIN", domain)
    namespace.setdefault("_command_generation", [0])
    namespace.setdefault("_tesla_reserve_generation", [0])
    namespace.setdefault("_local_powerwall_runtime", hass.data.get(domain, {}).get(entry.entry_id))
    if "_update_powerwall_store" not in namespace:
        load_power_sync_function("_update_powerwall_store", namespace)
    if "_local_powerwall_generation_current" not in namespace:
        load_power_sync_function("_local_powerwall_generation_current", namespace)
    return namespace


def real_tesla_ble_helpers():
    """Return the real ``tesla_ble`` helpers, bound to the real ``const`` values.

    ``tesla_ble.py`` only needs stdlib plus literal ``TESLA_BLE_*`` constants,
    so exec-namespace tests should seed these instead of hand-written stubs.
    """
    import ast
    import pathlib

    component = pathlib.Path(__file__).resolve().parents[1] / "custom_components/power_sync"
    namespace = {}
    for node in ast.parse((component / "const.py").read_text()).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("TESLA_BLE_")):
            namespace[node.targets[0].id] = ast.literal_eval(node.value)
    path = component / "tesla_ble.py"
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.level)]
    exec(compile(tree, str(path), "exec"), namespace)
    return {node.name: namespace[node.name] for node in tree.body
            if isinstance(node, ast.FunctionDef)}
