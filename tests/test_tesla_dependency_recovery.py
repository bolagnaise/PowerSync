"""Recoverable diagnostics for Tesla dependencies surviving a PowerSync update."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


def test_tesla_fleet_failure_cannot_block_power_sync_startup():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())

    assert "tesla_fleet" not in manifest.get("dependencies", [])
    assert "tesla_fleet" not in manifest.get("after_dependencies", [])


@pytest.fixture
def recovery(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "ps_tesla_dependency_recovery", COMPONENT / "tesla_dependencies.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tesla_token_reader():
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_tesla_api_token"
    )
    module = ast.Module(
        body=ast.parse("from __future__ import annotations").body + [function],
        type_ignores=[],
    )
    namespace = {
        "CONF_TESLA_API_PROVIDER": "tesla_api_provider",
        "CONF_TESLEMETRY_API_TOKEN": "teslemetry_api_token",
        "CONF_TOKEN": "token",
        "CONF_ACCESS_TOKEN": "access_token",
        "TESLA_PROVIDER_TESLEMETRY": "teslemetry",
        "TESLA_PROVIDER_FLEET_API": "fleet_api",
        "TESLA_PROVIDER_POWERSYNC": "powersync",
        "ConfigEntryState": SimpleNamespace(LOADED="loaded"),
        "_LOGGER": SimpleNamespace(warning=lambda *args, **kwargs: None),
    }
    exec(
        compile(ast.fix_missing_locations(module), "power_sync/__init__.py", "exec"),
        namespace,
    )
    return namespace["get_tesla_api_token"]


def _install_metadata(monkeypatch, recovery, *, protocol="1.4.0", protobuf="7.36.0", fleet_requires=None):
    # Published HA 2026.10.1 dependency metadata, including the distinct v4
    # energy-library conflict. Top-level versions remain correctly installed.
    distributions = {
        "tesla-fleet-api": SimpleNamespace(
            version="1.17.3", requires=fleet_requires or ["tesla-protocol>=3.0.0", "protobuf>=6.33.5"]
        ),
        "aiopowerwall": SimpleNamespace(version="0.4.1", requires=["tesla-protocol>=1.4.0,<4"]),
        "tesla-protocol": SimpleNamespace(version=protocol, requires=["protobuf>=4.25.0"]),
        "protobuf": SimpleNamespace(version=protobuf, requires=[]),
    }

    def distribution(name):
        if name not in distributions or distributions[name].version is None:
            raise metadata.PackageNotFoundError(name)
        return distributions[name]

    monkeypatch.setattr(recovery.metadata, "distribution", distribution)
    monkeypatch.setattr(recovery.metadata, "version", lambda name: distribution(name).version)
    return distributions


@pytest.mark.parametrize("protocol", ["1.3.1", "1.4.0"])
def test_removing_power_sync_requirement_does_not_repair_installed_protocol(recovery, monkeypatch, protocol):
    requirements = json.loads((COMPONENT / "manifest.json").read_text())["requirements"]
    assert not any("tesla-protocol" in requirement for requirement in requirements)
    _install_metadata(monkeypatch, recovery, protocol=protocol)
    before = {name for name in sys.modules if name.startswith("tesla_protocol")}
    check = recovery.inspect_tesla_dependencies({"tesla-fleet-api"})
    assert check.complete
    assert check.conflicts == (f"tesla-fleet-api 1.17.3 requires tesla-protocol>=3.0.0; installed {protocol}",)
    assert {name for name in sys.modules if name.startswith("tesla_protocol")} == before


def test_newer_protocol_can_break_energy_client_independently(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery, protocol="4.0.0")
    check = recovery.inspect_tesla_dependencies({"tesla-fleet-api", "aiopowerwall"})
    assert len(check.conflicts) == 1
    assert check.conflicts[0].startswith("aiopowerwall 0.4.1 requires tesla-protocol")
    assert check.conflicts[0].endswith("installed 4.0.0")


def test_rebuilt_compatible_environment_has_no_conflicts(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery, protocol="3.0.0")
    assert recovery.inspect_tesla_dependencies({"tesla-fleet-api", "aiopowerwall"}) == recovery.DependencyCheck((), True)


def test_follows_new_consumer_contract_instead_of_pinning_protocol(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery, protocol="4.0.0", fleet_requires=["tesla-protocol>=4.0.0"])
    assert recovery.inspect_tesla_dependencies({"tesla-fleet-api"}).conflicts == ()


def test_markers_and_unrelated_optional_dependencies_do_not_trigger(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery, fleet_requires=[
        'tesla-protocol>=4; python_version<"3.0"', 'tesla-protocol>=4; extra=="optional"', "uninstalled-library>=1"
    ])
    assert recovery.inspect_tesla_dependencies({"tesla-fleet-api"}) == recovery.DependencyCheck((), True)


def test_checks_protocol_runtime_requirements_without_importing_it(recovery, monkeypatch):
    distributions = _install_metadata(monkeypatch, recovery, protocol="3.0.0")
    distributions["tesla-protocol"].requires = ["protobuf>=8"]
    check = recovery.inspect_tesla_dependencies({"tesla-fleet-api"})
    assert check.conflicts == ("tesla-protocol 3.0.0 requires protobuf>=8; installed 7.36.0",)


def test_missing_dependency_is_a_conflict(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery, protocol=None)
    check = recovery.inspect_tesla_dependencies({"tesla-fleet-api"})
    assert check.complete
    assert check.conflicts == ("tesla-fleet-api 1.17.3 requires tesla-protocol>=3.0.0; installed missing",)


@pytest.mark.parametrize("damage", ["missing_consumer", "invalid_version", "bad_requirement"])
def test_unreadable_metadata_is_unknown(recovery, monkeypatch, damage):
    distributions = _install_metadata(monkeypatch, recovery)
    if damage == "missing_consumer":
        del distributions["tesla-fleet-api"]
    elif damage == "invalid_version":
        distributions["tesla-protocol"].version = "invalid"
    else:
        distributions["tesla-fleet-api"].requires = ["invalid%%%"]
    assert not recovery.inspect_tesla_dependencies({"tesla-fleet-api"}).complete


def _hass_and_registry(monkeypatch, domains):
    calls = []
    registry = SimpleNamespace(
        IssueSeverity=SimpleNamespace(ERROR="error"),
        async_create_issue=lambda *args, **kwargs: calls.append(("create", args, kwargs)),
        async_delete_issue=lambda *args, **kwargs: calls.append(("delete", args, kwargs)),
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", SimpleNamespace(issue_registry=registry))

    async def executor(fn, *args):
        return fn(*args)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda: [
            SimpleNamespace(domain=domain, disabled_by=None) for domain in domains
        ]),
        async_add_executor_job=executor,
    )
    return hass, calls


@pytest.mark.parametrize("domain", ["tesla_fleet", "teslemetry", "tessie"])
def test_active_failed_client_gets_one_repair_and_recovery_clears_it(recovery, monkeypatch, domain):
    distributions = _install_metadata(monkeypatch, recovery)
    hass, calls = _hass_and_registry(monkeypatch, [domain, "power_sync"])
    asyncio.run(recovery.async_check_tesla_dependencies(hass))
    assert len(calls) == 1
    assert calls[0][0] == "create"
    assert calls[0][1][1:] == ("power_sync", recovery.ISSUE_ID)
    assert "installed 1.4.0" in calls[0][2]["translation_placeholders"]["conflicts"]
    distributions["tesla-protocol"].version = "3.0.0"
    asyncio.run(recovery.async_check_tesla_dependencies(hass))
    assert calls[-1][0] == "delete"


def test_unknown_check_preserves_existing_repair(recovery, monkeypatch):
    distributions = _install_metadata(monkeypatch, recovery)
    del distributions["tesla-fleet-api"]
    hass, calls = _hass_and_registry(monkeypatch, ["tesla_fleet"])
    asyncio.run(recovery.async_check_tesla_dependencies(hass))
    assert calls == []


def test_executor_failure_does_not_break_startup_or_clear_repair(recovery, monkeypatch):
    hass, calls = _hass_and_registry(monkeypatch, ["tesla_fleet"])

    async def failed_executor(*args):
        raise OSError("metadata unavailable")

    hass.async_add_executor_job = failed_executor
    asyncio.run(recovery.async_check_tesla_dependencies(hass))
    assert calls == []


def test_direct_url_contract_is_unknown(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery, fleet_requires=["tesla-protocol @ https://example.invalid/protocol.whl"])
    assert recovery.inspect_tesla_dependencies({"tesla-fleet-api"}) == recovery.DependencyCheck((), False)


def test_repair_text_matches_in_both_english_resources():
    sources = [json.loads((COMPONENT / relative).read_text()) for relative in ("strings.json", "translations/en.json")]
    issues = [source["issues"]["tesla_dependency_conflict"] for source in sources]
    assert issues[0] == issues[1]
    assert "ha core rebuild" in issues[0]["description"]
    assert "{conflicts}" in issues[0]["description"]


def test_unconfigured_and_disabled_clients_do_not_get_conflict_notice(recovery, monkeypatch):
    _install_metadata(monkeypatch, recovery)
    hass, calls = _hass_and_registry(monkeypatch, ["power_sync", "tesla_fleet"])
    hass.config_entries.async_entries = lambda: [
        SimpleNamespace(domain="tesla_fleet", disabled_by="user"), SimpleNamespace(domain="power_sync")
    ]
    asyncio.run(recovery.async_check_tesla_dependencies(hass))
    assert calls[0][0] == "delete"


def test_startup_callback_checks_dependencies_before_dashboard(monkeypatch):
    tree = ast.parse((COMPONENT / "__init__.py").read_text())
    setup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup")
    callback = next(node for node in setup.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_ha_started")
    calls = []

    async def check(hass):
        calls.append("dependency_check")

    async def resource(hass):
        calls.append("resource")

    async def dashboard(hass):
        calls.append("dashboard")

    monkeypatch.setitem(sys.modules, "startup_test.tesla_dependencies", SimpleNamespace(async_check_tesla_dependencies=check))
    namespace = {"__package__": "startup_test", "hass": object(), "_ensure_lovelace_resource": resource, "_ensure_powersync_dashboard": dashboard}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(COMPONENT / "__init__.py"), "exec"), namespace)
    asyncio.run(namespace["_on_ha_started"]())
    assert calls == ["dependency_check", "resource", "dashboard"]


def test_fleet_token_is_available_only_after_fleet_entry_loads(tesla_token_reader):
    fleet_entry = SimpleNamespace(
        state="setup_in_progress",
        data={"token": {"access_token": "fleet-access-token"}},
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [fleet_entry])
    )
    power_sync_entry = SimpleNamespace(data={"tesla_api_provider": "fleet_api"})

    assert tesla_token_reader(hass, power_sync_entry) == (None, "fleet_api")

    fleet_entry.state = "loaded"
    assert tesla_token_reader(hass, power_sync_entry) == (
        "fleet-access-token",
        "fleet_api",
    )


@pytest.mark.parametrize(
    "provider",
    ["powersync", "teslemetry"],
)
def test_independent_tesla_token_provider_works_while_fleet_is_unavailable(
    tesla_token_reader, provider
):
    hass = SimpleNamespace()
    power_sync_entry = SimpleNamespace(
        data={
            "tesla_api_provider": provider,
            "teslemetry_api_token": "independent-provider-token",
        }
    )

    assert tesla_token_reader(hass, power_sync_entry) == (
        "independent-provider-token",
        provider,
    )
