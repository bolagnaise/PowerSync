"""Tests for shared log redaction helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_sync"
    / "sensitive_logging.py"
)
_SPEC = importlib.util.spec_from_file_location("power_sync_sensitive_logging", _MODULE_PATH)
assert _SPEC is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
obfuscate_vin_tokens = _MODULE.obfuscate_vin_tokens


VIN = "LRWYHCFS3PC901374"
MASKED_VIN = "LRWY*********1374"


def _mask(value: str) -> str:
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def test_obfuscate_vin_tokens_masks_bare_vin_contexts() -> None:
    text = (
        "Auto-schedule status: {'LRWYHCFS3PC901374': False}; "
        "Multi-vehicle decision for Keksla (LRWYHCFS3PC901374); "
        "EV Coordinator: Vehicle LRWYHCFS3PC901374"
    )

    result = obfuscate_vin_tokens(text, _mask)

    assert VIN not in result
    assert result.count(MASKED_VIN) == 3


def test_obfuscate_vin_tokens_leaves_already_masked_values() -> None:
    text = "ChargingScheduleView: VIN LRWY*********1374"

    assert obfuscate_vin_tokens(text, _mask) == text


def test_obfuscate_vin_tokens_ignores_non_vin_tokens() -> None:
    text = "site 12345678901234567 and token ABCDEFGHJKLMNPRST"

    assert obfuscate_vin_tokens(text, _mask) == text
