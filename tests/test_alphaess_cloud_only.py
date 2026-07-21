"""Regression coverage for AlphaESS Cloud-only monitoring mode."""

import ast
import asyncio
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR = ROOT / "custom_components" / "power_sync" / "coordinator.py"
CONFIG_FLOW = ROOT / "custom_components" / "power_sync" / "config_flow.py"
INIT = ROOT / "custom_components" / "power_sync" / "__init__.py"
API = ROOT / "custom_components" / "power_sync" / "alphaess_api.py"


def _cloud_test_connection_method():
    tree = ast.parse(API.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AlphaESSCloudClient"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "test_connection"
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"AlphaESSCloudError": RuntimeError}
    exec(compile(module, str(API), "exec"), namespace)
    return namespace["test_connection"]


def test_cloud_only_coordinator_has_no_modbus_controller_or_dispatch():
    source = COORDINATOR.read_text()
    alpha = source[
        source.index("class AlphaESSEnergyCoordinator"):
        source.index("def _normalize_alphaess_cloud_data")
    ]

    assert 'self.supports_dispatch = connection_type != "cloud_only" and bool(host)' in alpha
    assert "if self.supports_dispatch:" in alpha
    assert "if self._controller is None:" in alpha
    assert "await self._cloud.get_last_power_data()" in alpha
    assert alpha.count("if not self.supports_dispatch or self._controller is None:") == 5


def test_cloud_only_setup_requires_cloud_credentials_and_forces_monitoring():
    flow = CONFIG_FLOW.read_text()
    runtime = INIT.read_text()

    assert "ALPHAESS_CONNECTION_CLOUD_ONLY" in flow
    assert 'errors["base"] = "alphaess_cloud_required"' in flow
    assert "connection_type=alphaess_connection_type" in runtime
    assert "and not alphaess_coordinator.supports_dispatch" in runtime
    assert "is_alphaess = bool(entry.data.get(CONF_ALPHAESS_MODBUS_HOST))" not in runtime


def test_single_alphaess_cloud_system_is_auto_selected_for_runtime_reads():
    method = _cloud_test_connection_method()

    class Client:
        serial = ""

        async def get_ess_list(self):
            return [{"sysSn": "ALPHA-123"}]

    client = Client()
    ok, _ = asyncio.run(method(client))

    assert ok is True
    assert client.serial == "ALPHA-123"


def test_multiple_alphaess_cloud_systems_require_an_explicit_serial():
    method = _cloud_test_connection_method()

    class Client:
        serial = ""

        async def get_ess_list(self):
            return [{"sysSn": "ONE"}, {"sysSn": "TWO"}]

    client = Client()
    ok, message = asyncio.run(method(client))

    assert ok is False
    assert "Enter the AlphaESS system serial number" in message
