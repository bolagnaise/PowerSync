"""Full reader matrix: passive sources, redaction, bounded files and cancellation."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import importlib
import json
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "powersync_full_read_adapter_tests"
package = ModuleType(NAME)
package.__path__ = [str(ROOT / "custom_components/power_sync/ha_read")]
sys.modules[NAME] = package
reader = importlib.import_module(NAME + ".reader")
adapters = importlib.import_module(NAME + ".adapters")
files = importlib.import_module(NAME + ".files")
CANARY = "SYNTHETIC_CREDENTIAL_NEVER_DISCLOSE_123456789"
NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


@pytest.fixture
def installation(tmp_path):
    modules, mutations = {}, []
    def forbidden(*args, **kwargs):
        mutations.append((args, kwargs))
        raise AssertionError("reader reached a mutation, refresh or generic callback")
    state = NS(entity_id="sensor.third_party", state="12", attributes={"friendly_name": "Third party"}, last_changed=NOW, last_updated=NOW)
    entry = NS(entry_id="entry", domain="other_integration", title="Other integration", state="loaded", version=1,
               minor_version=1, disabled_by=None, data={"password": CANARY, "host": "192.0.2.5"}, options={})
    config = NS(config_dir=str(tmp_path), components={"recorder", "automation", "script", "trace", "lovelace", "hassio"},
                as_dict=lambda: {"time_zone": "Australia/Sydney", "version": "2026.11.1", "components": ["recorder"]})
    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)
    hass = NS(config=config, data={}, config_entries=NS(async_entries=lambda: [entry]),
              states=NS(async_all=lambda: [state], get=lambda _: state, async_set=forbidden, async_remove=forbidden),
              services=NS(async_call=forbidden), bus=NS(async_fire=forbidden), async_add_executor_job=executor)
    @dataclass
    class RegistryEntry:
        id: str
        entity_id: str
        name: str
        options: dict
    for kind, collection in [("entity", "entities"), ("device", "devices"), ("area", "areas"), ("floor", "floors"), ("label", "labels")]:
        key = kind + "_registry"
        modules["homeassistant.helpers." + kind + "_registry"] = NS(DATA_REGISTRY=key, async_get=forbidden)
        hass.data[key] = NS(**{collection: {"id": RegistryEntry("id", state.entity_id, kind, {"api_key": CANARY})}})
    modules["homeassistant.components.automation.const"] = NS(DATA_COMPONENT="automation")
    for domain in ["automation", "script"]:
        modules["homeassistant.components." + domain] = NS(async_reload=forbidden)
        hass.data[domain] = NS(entities=[NS(entity_id=domain + ".example", raw_config={"alias": "Example", "sequence": [{"message": CANARY}], "password": CANARY})])
    modules["homeassistant.components.trace.const"] = NS(DATA_TRACE="trace")
    trace = NS(run_id="live", as_extended_dict=lambda: {"variables": {"password": CANARY}, "message": CANARY})
    hass.data["trace"] = {"automation.example": NS(all_traces=lambda: [trace])}
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage/trace.saved_traces").write_text(json.dumps({"version": 1, "data": {"automation.example": [{"run_id": "saved", "message": CANARY}]}}))
    (tmp_path / "configuration.yaml").write_text("sensor:\n  password: !secret owner_password\n  text: !include nested.yaml\n  template: '{{ states(\"sensor.example\") }}'\n")
    (tmp_path / "ui-lovelace.yaml").write_text("title: Example\nviews:\n  - password: !secret dashboard_secret\n")
    (tmp_path / "secrets.yaml").write_text("owner_password: NEVER_OPEN_THIS_SECRET_FILE\n")
    (tmp_path / "home-assistant.log").write_text(f"2026-11-13 INFO component ready\n2026-11-13 ERROR power_sync password={CANARY}\n")
    (tmp_path / ".storage/lovelace.storage").write_text(json.dumps({"version": 1, "data": {"config": {"title": "Storage", "password": CANARY}}}))
    modules["homeassistant.components.lovelace"] = NS(LOVELACE_DATA="lovelace")
    hass.data["lovelace"] = NS(dashboards={None: NS(_data=None, config={"filename": "ui-lovelace.yaml"}), "storage": NS(_data=None, config={})})
    hass.data["system_log"] = NS(records=NS(to_list=lambda: [{"name": "example", "message": [CANARY], "count": 1}]))
    modules["homeassistant.components.hassio"] = NS(get_info=lambda _: {"version": "2026.11.1"},
        get_host_info=lambda _: {"operating_system": "HA OS"}, get_addons_list=lambda _: [{"name": "Example", "state": "started"}],
        async_get_info=forbidden, async_send_command=forbidden)
    modules["homeassistant.components.backup"] = NS(async_get_manager=lambda _: NS(known_backups=NS(to_list=lambda: [{"backup_id": "known", "failed_agent_ids": []}])))
    history_calls = []
    def history(*args, **kwargs):
        assert kwargs == {"entity_id": state.entity_id, "no_attributes": False, "limit": 1001, "include_start_time_state": True}
        history_calls.append((args, kwargs))
        return {state.entity_id: [state]}
    modules["homeassistant.components.recorder.history"] = NS(state_changes_during_period=history)
    def statistics(hass, start, end, ids, period, units, types):
        assert ids == {state.entity_id} and types == {"mean", "min", "max", "state", "sum", "last_reset", "change"}
        return {state.entity_id: [{"start": start.timestamp(), "mean": 12.0, "sum": 1.5}]}
    modules["homeassistant.components.recorder.statistics"] = NS(statistics_during_period=statistics)
    def module(name):
        return modules[name]
    backend = adapters.create_backend(hass, version="2026.11.1", modules=module, wall=lambda: NOW.timestamp())
    context = reader.ReadContext("installation", "staff", "codex", 1, NOW.timestamp() + 3600, reader.TOOLS, lambda *a: True)
    return NS(hass=hass, modules=modules, backend=backend, context=context, root=tmp_path,
              reader_set=backend.adapters["ha_get_registry"].owner, mutations=mutations, state=state, entry=entry, history_calls=history_calls)


MATRIX = [
    ("ha_get_registry", {"registry": "entity"}), ("ha_get_history", {"entity_ids": ["sensor.third_party"]}),
    ("ha_get_statistics", {"statistic_ids": ["sensor.third_party"]}), ("ha_get_logbook", {}),
    ("ha_get_automations", {}), ("ha_get_scripts", {}), ("ha_get_trace", {"domain": "automation"}),
    ("ha_get_integration_config", {}), ("ha_get_dashboard", {}),
    ("ha_read_configuration", {"path": "configuration.yaml"}), ("ha_get_logs", {"source": "core"}),
    ("ha_get_system", {}), ("ha_get_supervisor", {"section": "info"}),
]


def install_database(installation):
    from sqlalchemy import create_engine, Integer, Float, Text, String
    from sqlalchemy.orm import DeclarativeBase, Session, mapped_column
    from sqlalchemy.pool import StaticPool
    class Base(DeclarativeBase):
        pass
    class Events(Base):
        __tablename__ = "events"
        event_id = mapped_column(Integer, primary_key=True)
        time_fired_ts = mapped_column(Float)
        event_type_id = mapped_column(Integer)
        data_id = mapped_column(Integer)
    class EventTypes(Base):
        __tablename__ = "event_types"
        event_type_id = mapped_column(Integer, primary_key=True)
        event_type = mapped_column(String)
    class EventData(Base):
        __tablename__ = "event_data"
        data_id = mapped_column(Integer, primary_key=True)
        shared_data = mapped_column(Text)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([EventTypes(event_type_id=1, event_type="logbook_entry"),
            EventData(data_id=1, shared_data=json.dumps({"entity_id": "sensor.third_party", "message": CANARY})),
            Events(event_id=1, time_fired_ts=NOW.timestamp() - 5, event_type_id=1, data_id=1)])
        session.commit()
    @contextmanager
    def session_scope(*, hass, read_only):
        assert hass is installation.hass and read_only is True
        with Session(engine) as session:
            session.execute(importlib.import_module("sqlalchemy").text("PRAGMA query_only=ON"))
            yield session
    installation.modules["homeassistant.components.recorder.db_schema"] = NS(Events=Events, EventTypes=EventTypes, EventData=EventData)
    installation.modules["homeassistant.helpers.recorder"] = NS(session_scope=session_scope)
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", MATRIX)
async def test_all_thirteen_real_adapters_are_passive_and_redact_credentials(installation, operation, arguments):
    engine = install_database(installation) if operation == "ha_get_logbook" else None
    try:
        before = installation.entry.data.copy()
        answer = await installation.backend.read(installation.context, operation, arguments)
        assert answer["result"]["status"] == "available"
        assert answer["result"]["records"]
        assert CANARY not in json.dumps(answer)
        assert "NEVER_OPEN_THIS_SECRET_FILE" not in json.dumps(answer)
        assert installation.entry.data == before and installation.mutations == []
    finally:
        if engine:
            engine.dispose()


@pytest.mark.asyncio
async def test_capabilities_cover_complete_matrix_and_optional_absence(installation):
    answer = (await installation.backend.read(installation.context, "ha_get_capabilities"))["result"]
    assert answer["complete_profile"] is True and set(answer["supported"]) == reader.TOOLS
    installation.hass.config.components.remove("hassio")
    answer = (await installation.backend.read(installation.context, "ha_get_capabilities"))["result"]
    assert "ha_get_supervisor" in answer["unavailable"]
    missing = (await installation.backend.read(installation.context, "ha_get_supervisor"))["result"]
    assert missing["unavailable_reason"] == "component_not_installed"
    installation.reader_set.version = "2026.12.0"
    answer = (await installation.backend.read(installation.context, "ha_get_capabilities"))["result"]
    assert answer["complete_profile"] is False
    assert set(answer["supported"]) == {"ha_get_capabilities", "ha_search_entities", "ha_get_states"}


@pytest.mark.asyncio
async def test_registry_cursor_rejects_changed_inventory_and_other_clients(installation):
    registry = installation.hass.data["entity_registry"].entities
    original = registry["id"]
    registry.update({f"id{i}": type(original)(f"id{i}", f"sensor.device_{i}", "Example", {}) for i in range(1200)})
    page = (await installation.backend.read(installation.context, "ha_get_registry", {"limit": 200}))["result"]
    assert len(page["records"]) == 200 and page["next_cursor"]
    args = {"limit": 200, "cursor": page["next_cursor"]}
    other = reader.ReadContext("installation", "staff", "claude", 1, NOW.timestamp() + 3600, reader.TOOLS, lambda *a: True)
    with pytest.raises(reader.ReadError, match="invalid_or_stale_cursor"):
        await installation.backend.read(other, "ha_get_registry", args)
    registry["id"].name = "Changed"
    with pytest.raises(reader.ReadError, match="invalid_or_stale_cursor"):
        await installation.backend.read(installation.context, "ha_get_registry", args)


@pytest.mark.asyncio
async def test_secondary_registry_supervisor_dashboard_and_system_log_views(installation):
    for kind in ["device", "area", "floor", "label"]:
        answer = await installation.backend.read(installation.context, "ha_get_registry", {"registry": kind})
        assert answer["result"]["records"][0]["name"] == kind
    for section in ["host", "addons", "backup_metadata"]:
        answer = await installation.backend.read(installation.context, "ha_get_supervisor", {"section": section})
        assert answer["result"]["records"]
    for operation, args in [("ha_get_dashboard", {"url_path": "storage"}), ("ha_get_logs", {"source": "system"}), ("ha_get_logs", {"source": "power_sync"})]:
        answer = await installation.backend.read(installation.context, operation, args)
        assert CANARY not in json.dumps(answer)
        assert answer["result"]["records"]


@pytest.mark.asyncio
async def test_history_and_statistics_limits_precede_executor_work(installation):
    for operation, args in [
        ("ha_get_history", {"entity_ids": ["sensor.third_party", "sensor.other"]}),
        ("ha_get_history", {"entity_ids": ["sensor.third_party"], "start": NOW.timestamp() - 90000}),
        ("ha_get_statistics", {"statistic_ids": ["sensor.third_party"], "period": "5minute", "start": NOW.timestamp() - 366 * 86400}),
    ]:
        with pytest.raises(reader.ReadError, match="invalid_query"):
            await installation.backend.read(installation.context, operation, args)
    assert installation.history_calls == [] and not installation.reader_set.inflight


def test_managed_files_deny_secrets_traversal_symlinks_and_executable_yaml(installation, tmp_path):
    managed = files.ManagedFiles(installation.root)
    (installation.root / "escape.yaml").symlink_to("/etc/passwd")
    for name in ["secrets.yaml", ".storage/auth", "../configuration.yaml", "/etc/passwd", "escape.yaml"]:
        with pytest.raises(reader.ReadError):
            managed.configuration(name)
    marker = tmp_path / "must-not-exist"
    document = f'!!python/object/apply:os.system ["touch {marker}"]'
    result = files.yaml_nodes(document.encode())
    assert not marker.exists()
    assert result == {"unavailable": True, "reason": "unsupported_yaml_tag"}
    with pytest.raises(reader.ReadError):
        files.yaml_nodes(b"cycle: &cycle {again: *cycle}")
    with pytest.raises(reader.ReadError):
        files.yaml_nodes(b"key: first\nkey: second")


@pytest.mark.asyncio
async def test_large_configuration_pages_only_redacted_fragments(installation):
    (installation.root / "large.yaml").write_text("items:\n" + "".join(f"  - name: item_{i}\n    value: {'x' * 60}\n" for i in range(1800)))
    args = {"path": "large.yaml", "limit": 2}
    pieces = []
    while True:
        page = (await installation.backend.read(installation.context, "ha_read_configuration", args))["result"]
        pieces.extend(row["content"] for row in page["records"])
        if not page["next_cursor"]:
            break
        args["cursor"] = page["next_cursor"]
    document = json.loads("".join(pieces))
    assert len(document["configuration"]["items"]) == 1800


@pytest.mark.asyncio
async def test_cancelled_executor_keeps_its_slot_and_revocation_blocks_disclosure(installation):
    entered, release = Event(), Event()
    def blocked():
        entered.set()
        assert release.wait(5)
        return "private"
    task = asyncio.create_task(installation.reader_set.execute(blocked))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(installation.reader_set.inflight) == 1
    assert installation.reader_set.executor_slots._value == 1
    release.set()
    await asyncio.gather(*installation.reader_set.inflight)
    await asyncio.sleep(0)
    assert installation.reader_set.executor_slots._value == 2
