"""EV refusal record: every refused start leaves a queryable, logged reason."""

from __future__ import annotations

import ast
import importlib
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

if "power_sync" not in sys.modules:
    _ps = types.ModuleType("power_sync")
    _ps.__path__ = [str(ROOT)]
    sys.modules["power_sync"] = _ps
if "power_sync.automations" not in sys.modules:
    _automations = types.ModuleType("power_sync.automations")
    _automations.__path__ = [str(ROOT / "automations")]
    sys.modules["power_sync.automations"] = _automations

ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")

VIN = "5YJTEST0000000001"


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _Hass:
    def __init__(self) -> None:
        self.data = {"power_sync": {"entry-1": {}}}

    def async_create_task(self, coro):
        coro.close()


def test_failed_command_survives_a_later_success_and_is_logged(caplog):
    hass = _Hass()
    caplog.set_level(logging.INFO, logger=ev_ownership.__name__)

    ev_ownership.record_ev_command(
        hass, _Entry(), VIN, command="start_solar_surplus", success=False,
        reason="Waiting for battery",
    )
    ev_ownership.record_ev_command(
        hass, _Entry(), VIN, command="start_solar_surplus", success=True,
    )

    refusal = ev_ownership.latest_ev_refusal(hass, _Entry(), VIN)
    assert refusal["reason"] == "Waiting for battery"
    assert refusal["command"] == "start_solar_surplus"
    assert refusal["count"] == 1
    assert "EV decision:" in caplog.text and "Waiting for battery" in caplog.text


def test_repeated_identical_refusal_is_counted_but_not_relogged(caplog):
    hass = _Hass()
    caplog.set_level(logging.INFO, logger=ev_ownership.__name__)
    for _ in range(5):
        ev_ownership.record_ev_refusal(
            hass, _Entry(), VIN, command="start_smart_schedule", reason="busy",
        )

    assert ev_ownership.latest_ev_refusal(hass, _Entry(), VIN)["count"] == 5
    assert caplog.text.count("EV decision:") == 1
    assert ev_ownership.ev_refusal_sequence(hass, _Entry()) == 5


def test_unchanged_refusal_relogs_after_repeat_window(caplog):
    hass = _Hass()
    caplog.set_level(logging.INFO, logger=ev_ownership.__name__)
    ev_ownership.record_ev_refusal(
        hass, _Entry(), VIN, command="start_smart_schedule", reason="busy",
    )
    stored = ev_ownership.get_ev_last_refusals(hass, _Entry())[VIN]
    stored["logged_at"] = (
        datetime.now(timezone.utc)
        - timedelta(seconds=ev_ownership.EV_REFUSAL_LOG_REPEAT_SECONDS + 1)
    ).isoformat()

    ev_ownership.record_ev_refusal(
        hass, _Entry(), VIN, command="start_smart_schedule", reason="busy",
    )

    assert caplog.text.count("EV decision:") == 2


def test_new_reason_resets_count():
    hass = _Hass()
    ev_ownership.record_ev_refusal(hass, _Entry(), VIN, command="start_x", reason="a")
    ev_ownership.record_ev_refusal(hass, _Entry(), VIN, command="start_x", reason="b")

    refusal = ev_ownership.latest_ev_refusal(hass, _Entry(), VIN)
    assert refusal["reason"] == "b" and refusal["count"] == 1


def test_dynamic_start_wrapper_records_a_fallback_reason():
    """Every False from the locked start must leave a refusal behind."""
    source = (ROOT / "automations" / "actions.py").read_text()
    tree = ast.parse(source)
    wrapper = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_action_start_ev_charging_dynamic"
    )
    body = ast.unparse(wrapper)
    assert "ev_refusal_sequence(hass, config_entry) == refusal_seq" in body
    assert "record_ev_refusal(" in body
