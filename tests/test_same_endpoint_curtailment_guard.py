"""Regression coverage for the same-endpoint AC-curtailment runtime guard (OB-26).

The Sungrow AC-inverter curtailment path must never open a second Modbus client
against the battery's own SH/WiNet-S endpoint. The config flow rejects such a
config, but old bad configs survive upgrades, so ``ac_inverter_is_same_hybrid``
is the runtime safety net. It must skip curtailment for ANY same-endpoint Sungrow
config, regardless of the model string -- the model name is not a reliable signal
(pre-fix it only fired for models starting with ``sh``).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"

# Stable Home Assistant config keys referenced by ac_inverter_is_same_hybrid.
# Mirrors the hard-coded-namespace pattern in test_sungrow_curtailment_runtime.py
# so the extracted function can run without importing the HA-dependent package.
_CONST_NAMESPACE = {
    "CONF_INVERTER_BRAND": "inverter_brand",
    "CONF_INVERTER_MODEL": "inverter_model",
    "CONF_INVERTER_HOST": "inverter_host",
    "CONF_INVERTER_PORT": "inverter_port",
    "CONF_INVERTER_SLAVE_ID": "inverter_slave_id",
    "DEFAULT_INVERTER_PORT": 502,
    "DEFAULT_INVERTER_SLAVE_ID": 1,
    "CONF_SUNGROW_HOST": "sungrow_host",
    "CONF_SUNGROW_PORT": "sungrow_port",
    "CONF_SUNGROW_SLAVE_ID": "sungrow_slave_id",
    "DEFAULT_SUNGROW_PORT": 502,
    "DEFAULT_SUNGROW_SLAVE_ID": 1,
}

# The battery's own Sungrow SH / WiNet-S Modbus endpoint.
_BATTERY_HOST = "192.168.1.50"
_BATTERY_PORT = 502
_BATTERY_SLAVE = 1


def _nested_function_source(source_path: Path, name: str) -> str:
    """Extract a (possibly nested) function definition by name from a source file."""
    source = source_path.read_text()
    module = ast.parse(source)
    for node in ast.walk(module):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found in {source_path}")


def _build_guard(entry, source_path: Path = INIT_PATH):
    namespace = dict(_CONST_NAMESPACE)
    namespace["entry"] = entry
    exec(
        textwrap.dedent(
            _nested_function_source(source_path, "ac_inverter_is_same_hybrid")
        ),
        namespace,
    )
    return namespace["ac_inverter_is_same_hybrid"]


def _entry(*, brand="sungrow", model, host, port, slave):
    """Build a fake config entry with the given AC-inverter target."""
    return SimpleNamespace(
        options={},
        data={
            "inverter_brand": brand,
            "inverter_model": model,
            "inverter_host": host,
            "inverter_port": port,
            "inverter_slave_id": slave,
            "sungrow_host": _BATTERY_HOST,
            "sungrow_port": _BATTERY_PORT,
            "sungrow_slave_id": _BATTERY_SLAVE,
        },
    )


def test_same_endpoint_non_sh_model_is_treated_as_same_hybrid():
    """(a) Same host/port/slave as the battery, NON-SH model -> skip curtailment.

    This FAILS against pre-fix HEAD, where the model gate short-circuited to
    False for any model not starting with "sh".
    """
    guard = _build_guard(
        _entry(model="sg5.0rs", host=_BATTERY_HOST, port=_BATTERY_PORT, slave=_BATTERY_SLAVE)
    )
    assert guard() is True


def test_same_endpoint_empty_model_is_treated_as_same_hybrid():
    """(a') Same endpoint, empty/unknown model -> still skip curtailment."""
    guard = _build_guard(
        _entry(model="", host=_BATTERY_HOST, port=_BATTERY_PORT, slave=_BATTERY_SLAVE)
    )
    assert guard() is True


def test_separate_inverter_is_not_same_hybrid():
    """(b) Genuinely separate inverter (different host/port/slave) -> allow curtailment."""
    # Different host.
    assert (
        _build_guard(
            _entry(model="sh10rs", host="192.168.1.99", port=_BATTERY_PORT, slave=_BATTERY_SLAVE)
        )()
        is False
    )
    # Different port.
    assert (
        _build_guard(
            _entry(model="sg5.0rs", host=_BATTERY_HOST, port=503, slave=_BATTERY_SLAVE)
        )()
        is False
    )
    # Different slave id.
    assert (
        _build_guard(
            _entry(model="sg5.0rs", host=_BATTERY_HOST, port=_BATTERY_PORT, slave=2)
        )()
        is False
    )


def test_sh_same_endpoint_still_same_hybrid():
    """(c) Original SH same-endpoint case still detected -> skip curtailment."""
    guard = _build_guard(
        _entry(model="sh10rs", host=_BATTERY_HOST, port=_BATTERY_PORT, slave=_BATTERY_SLAVE)
    )
    assert guard() is True


def test_non_sungrow_ac_inverter_is_never_same_hybrid():
    """A non-Sungrow AC inverter is never the battery's own hybrid endpoint."""
    guard = _build_guard(
        _entry(
            brand="solax",
            model="x1-hybrid",
            host=_BATTERY_HOST,
            port=_BATTERY_PORT,
            slave=_BATTERY_SLAVE,
        )
    )
    assert guard() is False
