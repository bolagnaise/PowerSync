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
