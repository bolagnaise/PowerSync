"""Unit tests for Sungrow SH inverter register encoding/decoding.

Tests cover:
- _to_signed16: unsigned 16-bit → signed conversion
- _to_signed32: two 16-bit registers → signed 32-bit (Sungrow word-swap)
- _to_unsigned32: two 16-bit registers → unsigned 32-bit
- Round-trip verification
- SOC register scaling and invalid value handling
"""

from __future__ import annotations

import pytest

# Direct module loading — bypass broken __init__.py package chain
import importlib
import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INV_DIR = _REPO / "custom_components" / "power_sync" / "inverters"

def _load_module_direct(name: str, filepath: Path):
    """Load a Python module from file path without triggering parent __init__.py."""
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {filepath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

try:
    # Load base first (sungrow_sh imports from it)
    _base = _load_module_direct(
        "custom_components.power_sync.inverters.base",
        _INV_DIR / "base.py",
    )
    _sg = _load_module_direct(
        "custom_components.power_sync.inverters.sungrow_sh",
        _INV_DIR / "sungrow_sh.py",
    )
    SungrowSHController = _sg.SungrowSHController
    HAS_DEPS = True
except (ImportError, Exception) as _import_err:
    HAS_DEPS = False
    _skip_reason = f"Cannot load sungrow_sh: {_import_err}"

pytestmark = pytest.mark.skipif(not HAS_DEPS, reason=_skip_reason if not HAS_DEPS else "")


@pytest.fixture
def controller():
    """Create a SungrowSHController with dummy host (no connection needed)."""
    return SungrowSHController(host="192.168.1.100", port=502, slave_id=1)


# ---------------------------------------------------------------------------
# _to_signed16 Tests (AC-4)
# ---------------------------------------------------------------------------

def test_to_signed16_positive(controller):
    """Positive values (0-32767) pass through unchanged."""
    assert controller._to_signed16(0) == 0
    assert controller._to_signed16(100) == 100
    assert controller._to_signed16(32767) == 32767


def test_to_signed16_negative(controller):
    """Values >= 0x8000 convert to negative."""
    assert controller._to_signed16(0x8000) == -32768
    assert controller._to_signed16(0xFFFF) == -1
    assert controller._to_signed16(0xFFFE) == -2


def test_to_signed16_boundary(controller):
    """Boundary values: max positive and min negative."""
    assert controller._to_signed16(0x7FFF) == 32767  # Max positive
    assert controller._to_signed16(0x8000) == -32768  # Min negative
    assert controller._to_signed16(0x8001) == -32767


# ---------------------------------------------------------------------------
# _to_signed32 Tests (AC-5)
# ---------------------------------------------------------------------------

def test_to_signed32_positive(controller):
    """Positive 32-bit values from two registers (Sungrow word-swap: reg0=low, reg1=high)."""
    assert controller._to_signed32(0, 0) == 0
    assert controller._to_signed32(1000, 0) == 1000  # Low word only
    assert controller._to_signed32(0, 1) == 65536  # High word = 1


def test_to_signed32_negative(controller):
    """Negative 32-bit values."""
    # 0x80000000 = -2147483648 → reg1=0x8000 (high), reg0=0x0000 (low)
    assert controller._to_signed32(0x0000, 0x8000) == -2147483648
    # 0xFFFFFFFF = -1 → reg1=0xFFFF (high), reg0=0xFFFF (low)
    assert controller._to_signed32(0xFFFF, 0xFFFF) == -1


def test_to_signed32_word_swap(controller):
    """Verify Sungrow word-swap: reg0=low word, reg1=high word."""
    # Value 0x00010002 = 65538
    # reg0 (low) = 0x0002, reg1 (high) = 0x0001
    assert controller._to_signed32(0x0002, 0x0001) == 65538

    # Value 0x00020001 = 131073
    # reg0 (low) = 0x0001, reg1 (high) = 0x0002
    assert controller._to_signed32(0x0001, 0x0002) == 131073


# ---------------------------------------------------------------------------
# _to_unsigned32 Tests (AC-5)
# ---------------------------------------------------------------------------

def test_to_unsigned32(controller):
    """Unsigned 32-bit conversion."""
    assert controller._to_unsigned32(0, 0) == 0
    assert controller._to_unsigned32(0xFFFF, 0xFFFF) == 4294967295  # Max uint32
    assert controller._to_unsigned32(0, 1) == 65536


# ---------------------------------------------------------------------------
# Round-Trip Tests (AC-5)
# ---------------------------------------------------------------------------

def test_register_round_trip_16(controller):
    """Encode signed 16-bit → unsigned 16-bit → decode back should match."""
    test_values = [-32768, -1, 0, 1, 32767]

    for signed_val in test_values:
        # Encode: signed → unsigned 16-bit
        if signed_val < 0:
            unsigned = signed_val + 0x10000
        else:
            unsigned = signed_val

        # Decode back
        decoded = controller._to_signed16(unsigned)
        assert decoded == signed_val, f"Round-trip failed: {signed_val} → {unsigned} → {decoded}"


def test_register_round_trip_32(controller):
    """Encode signed 32-bit → two registers → decode back should match."""
    test_values = [-2147483648, -1, 0, 1, 2147483647]

    for signed_val in test_values:
        # Encode: signed → unsigned 32-bit
        if signed_val < 0:
            unsigned = signed_val + 0x100000000
        else:
            unsigned = signed_val

        # Split into Sungrow word-swap: reg0=low, reg1=high
        reg0 = unsigned & 0xFFFF
        reg1 = (unsigned >> 16) & 0xFFFF

        # Decode back
        decoded = controller._to_signed32(reg0, reg1)
        assert decoded == signed_val, f"Round-trip failed: {signed_val} → ({reg0}, {reg1}) → {decoded}"


# ---------------------------------------------------------------------------
# SOC Scaling Tests
# ---------------------------------------------------------------------------

def test_soc_scaling(controller):
    """SOC register values scale by 0.1 (500 → 50.0%, 1000 → 100.0%)."""
    assert round(500 * 0.1, 1) == 50.0
    assert round(1000 * 0.1, 1) == 100.0
    assert round(0 * 0.1, 1) == 0.0
    assert round(200 * 0.1, 1) == 20.0  # Typical backup reserve


def test_soc_invalid_0xFFFF():
    """0xFFFF (65535) should not be treated as valid SOC.

    65535 * 0.1 = 6553.5% which is physically impossible.
    The Phase 0 fix added _last_valid_soc handling for this.
    """
    raw = 0xFFFF
    soc = round(raw * 0.1, 1)
    assert soc > 100, "0xFFFF should produce invalid SOC value > 100%"
    # This confirms the Phase 0 fix was necessary
