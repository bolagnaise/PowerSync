"""The optimiser status explains why a planned battery command is not applied."""
from __future__ import annotations

import ast
import asyncio
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_battery_controller_wrapper import _FailingServices, _Services, _load_controller_module

ROOT = Path(__file__).resolve().parent.parent
COORDINATOR = ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"


class _RaisingServices(_Services):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def async_call(self, *args, **kwargs):
        raise self.error


def test_gateway_timeout_is_recorded_and_cleared_by_next_success():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(services=_RaisingServices(
            RuntimeError("Timed out connecting to Powerwall gateway at 192.0.2.10")))
        controller = module.BatteryControllerWrapper(hass, "tesla")

        assert asyncio.run(controller.force_charge(30, 0)) is False
        failure = controller.recent_command_failure()
        assert failure["command"] == "force_charge"
        assert failure["reason"] == "gateway_unreachable"
        assert "Powerwall gateway" in failure["message"]

        hass.services = _Services({"success": True})
        assert asyncio.run(controller.force_charge(30, 0)) is True
        assert controller.recent_command_failure() is None
    finally:
        restore()


def test_unconfirmed_tesla_response_and_ev_headroom_refusal_are_classified():
    module, restore = _load_controller_module()
    try:
        hass = SimpleNamespace(services=_Services(
            {"success": False, "error": "Cloud tariff charging cannot reserve shared EV headroom"}))
        controller = module.BatteryControllerWrapper(hass, "tesla")
        assert asyncio.run(controller.force_charge(30, 0)) is False
        assert controller.recent_command_failure()["reason"] == "ev_headroom_reserved"

        hass.services = _Services(None)
        assert asyncio.run(controller.force_discharge(30, 0)) is False
        failure = controller.recent_command_failure()
        assert failure["command"] == "force_discharge"
        assert failure["reason"] == "command_failed"
        assert failure["message"] == "Command was not confirmed"
    finally:
        restore()


def test_success_of_other_command_keeps_failure_and_failure_expires(monkeypatch):
    module, restore = _load_controller_module()
    try:
        controller = module.BatteryControllerWrapper(SimpleNamespace(services=_FailingServices()), "solaredge")
        assert asyncio.run(controller.force_charge(30, 5000)) is False
        controller.hass.services = _Services()
        assert asyncio.run(controller.force_discharge(30, 5000)) is True
        assert controller.recent_command_failure()["command"] == "force_charge"

        now = module.time.monotonic()
        monkeypatch.setattr(module.time, "monotonic",
                            lambda: now + module.COMMAND_FAILURE_MAX_AGE_SECONDS + 1)
        assert controller.recent_command_failure() is None
    finally:
        restore()


def _planned_command_failure():
    tree = ast.parse(COORDINATOR.read_text())
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == "_planned_command_failure")
    source = textwrap.dedent(ast.get_source_segment(COORDINATOR.read_text(), node))
    dt_util = SimpleNamespace(utc_from_timestamp=lambda value: datetime.fromtimestamp(value, timezone.utc))
    namespace = {"Any": object, "dt_util": dt_util}
    exec(source, namespace)
    return namespace["_planned_command_failure"]


def _coordinator(failure):
    battery = SimpleNamespace(recent_command_failure=lambda: failure)
    return SimpleNamespace(_executor=SimpleNamespace(battery_controller=battery))


def test_status_reports_failure_only_for_the_planned_action():
    method = _planned_command_failure()
    failure = {"command": "force_charge", "reason": "gateway_unreachable",
               "message": "Timed out connecting to Powerwall gateway", "at": 0.0}

    reported = method(_coordinator(failure), "charge")
    assert reported == {"command": "force_charge", "reason": "gateway_unreachable",
                        "message": "Timed out connecting to Powerwall gateway",
                        "at": "1970-01-01T00:00:00+00:00"}
    assert method(_coordinator(failure), "self_consumption") is None
    assert method(_coordinator(failure), "export") is None
    assert method(_coordinator({**failure, "command": "force_discharge"}), "export")["command"] == "force_discharge"
    assert method(_coordinator(None), "charge") is None
    assert method(SimpleNamespace(_executor=None), "charge") is None


def test_status_sensor_exposes_command_failure_attribute():
    sensor = (ROOT / "custom_components" / "power_sync" / "sensor.py").read_text()
    assert '"command_failure": data.get("command_failure")' in sensor
    assert '"command_failure": self._planned_command_failure(planned_current_action)' in COORDINATOR.read_text()
