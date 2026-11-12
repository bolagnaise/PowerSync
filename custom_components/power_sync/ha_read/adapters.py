"""Audited passive HA readers. API baseline: Home Assistant Core 2026.11.1.

No general API, SQL, template, URL or diagnostics-callback proxy is exposed.
Optional components and missing cached data are reported explicitly.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import hashlib
from importlib import import_module
import json
import math
import platform
import re

from .files import ManagedFiles, Redactor
from .reader import HAReadBackend, READERS, ReadError

API_BASELINE = "2026.11.1"
MAX_ROWS = 1000
ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
REGISTRY_FIELDS = frozenset({"id", "entity_id", "unique_id", "platform", "config_entry_id", "config_entries",
    "device_id", "area_id", "floor_id", "labels", "disabled_by", "hidden_by", "name", "original_name",
    "name_by_user", "icon", "entity_category", "unit_of_measurement", "capabilities", "options", "supported_features",
    "translation_key", "connections", "identifiers", "manufacturer", "model", "model_id", "sw_version",
    "hw_version", "serial_number", "via_device_id", "configuration_url", "aliases", "picture", "description"})


def stamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def stored(obj, names):
    """Select data fields, never enumerate or evaluate unknown properties."""
    if type(obj) is dict:
        return {name: obj[name] for name in names if name in obj}
    if is_dataclass(obj):
        declared = {field.name for field in fields(obj)}
        return {name: getattr(obj, name) for name in names & declared}
    values = getattr(obj, "__dict__", {})
    return {name: values[name] for name in names if name in values}


def state_record(state):
    context = getattr(state, "context", None)
    return {"entity_id": state.entity_id, "state": state.state, "attributes": dict(state.attributes),
            "last_changed": stamp(state.last_changed), "last_updated": stamp(state.last_updated),
            "context": stored(context, {"id", "parent_id", "user_id"}) if context else None}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=stamp, allow_nan=False).encode()).hexdigest()


def unavailable(reason):
    return {"status": "unavailable", "unavailable_reason": reason, "records": [],
            "coverage": "unavailable", "next_cursor": None}


class LocalReader:
    def __init__(self, owner, operation):
        self.owner, self.operation = owner, operation

    def status(self):
        return self.owner.status(self.operation)

    async def __call__(self, context, arguments):
        return await self.owner.read(context, self.operation, arguments)


class LocalReaders:
    def __init__(self, backend, *, version=None, modules=import_module):
        self.backend, self.hass, self.modules = backend, backend.hass, modules
        self.version = version if version is not None else modules("homeassistant.const").__version__
        self.executor_slots = asyncio.Semaphore(2)
        self.inflight = set()
        self.files = ManagedFiles(self.hass.config.config_dir)

    def status(self, operation):
        # The audited minor family is explicit. Future internal API revisions
        # retain cached states and require a new compatibility audit for adapters.
        compatible = bool(re.fullmatch(r"2026\.11\.\d+(?:b\d+)?", self.version))
        result = {"implemented": True, "available": compatible, "api_baseline": API_BASELINE}
        if not compatible:
            return {**result, "reason": "unsupported_ha_version"}
        component = {"ha_get_history": "recorder", "ha_get_statistics": "recorder", "ha_get_logbook": "recorder",
                     "ha_get_automations": "automation", "ha_get_scripts": "script", "ha_get_trace": "trace",
                     "ha_get_dashboard": "lovelace", "ha_get_supervisor": "hassio"}.get(operation)
        if component and component not in self.hass.config.components:
            return {**result, "available": False, "reason": "component_not_installed"}
        return result

    async def execute(self, function, *arguments):
        """Cancellation does not free an executor slot while its work continues."""
        await self.executor_slots.acquire()
        try:
            task = asyncio.ensure_future(self.hass.async_add_executor_job(function, *arguments))
        except BaseException:
            self.executor_slots.release()
            raise
        self.inflight.add(task)
        def finished(done):
            self.inflight.discard(done)
            self.executor_slots.release()
            if not done.cancelled():
                done.exception()  # Consume a late failure after caller cancellation.
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def redactor(self):
        entries = self.hass.config_entries.async_entries()
        if len(entries) > 2000:
            raise ReadError("result_limit")
        return Redactor([dict(value) for entry in entries for value in (entry.data, entry.options)])

    def page(self, context, operation, arguments, rows, redactor, **metadata):
        if len(rows) > 20000:
            raise ReadError("result_limit")
        # Split unusually large structured documents into independently bounded
        # JSON fragments only after projection/redaction, never raw source text.
        projected = []
        for row in rows:
            value = redactor.project(row)
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > 48000:
                identity = {key: value[key] for key in ("entity_id", "path", "run_id") if key in value}
                for index in range(0, len(encoded), 8000):
                    projected.append({**identity, "format": "redacted_json_fragment", "part": index // 8000,
                                      "parts": math.ceil(len(encoded) / 8000), "content": encoded[index:index + 8000]})
            else:
                projected.append(value)
        revision = digest(projected)
        return self.backend.page(context, operation, arguments, projected, revision=revision,
            metadata={"status": "available", "observed_at": self.backend.wall(), "consistency": "per_request_source_snapshot",
                      "coverage": "bounded_source_read", "redactions": redactor.redactions, **metadata})

    def window(self, args, *, max_seconds=86400):
        end = args.get("end", self.backend.wall())
        start = args.get("start", end - 600)
        if not 0 <= start < end <= self.backend.wall() + 1 or end - start > max_seconds:
            raise ReadError("invalid_query")
        return datetime.fromtimestamp(start, timezone.utc), datetime.fromtimestamp(end, timezone.utc)

    def entity_ids(self, args, *, single=False):
        ids = args.get("entity_ids")
        if not ids or len(ids) > (1 if single else 25) or any(not ENTITY.fullmatch(value) for value in ids):
            raise ReadError("invalid_query")
        return sorted(set(ids))

    async def read(self, context, operation, args):
        status = self.status(operation)
        if not status["available"]:
            return unavailable(status["reason"])
        redactor = self.redactor()
        if operation == "ha_get_registry":
            kind = args.get("registry", "entity")
            module = self.modules(f"homeassistant.helpers.{kind}_registry")
            registry = self.hass.data.get(module.DATA_REGISTRY)
            if registry is None:
                return unavailable("registry_cache_unavailable")
            values = getattr(registry, {"entity": "entities", "device": "devices", "area": "areas", "floor": "floors", "label": "labels"}[kind])
            if len(values) > 20000:
                raise ReadError("result_limit")
            rows = [stored(value, REGISTRY_FIELDS) for _, value in sorted(values.items())]
            return self.page(context, operation, args, rows, redactor, source="ha_registry_cache", registry=kind)
        if operation in {"ha_get_automations", "ha_get_scripts"}:
            domain = "automation" if operation == "ha_get_automations" else "script"
            module = self.modules(f"homeassistant.components.{domain}")
            key = self.modules("homeassistant.components.automation.const").DATA_COMPONENT if domain == "automation" else "script"
            component = self.hass.data.get(key)
            if component is None:
                return unavailable("component_cache_unavailable")
            selected = args.get("entity_id")
            if selected and (not ENTITY.fullmatch(selected) or not selected.startswith(domain + ".")):
                raise ReadError("invalid_query")
            rows = []
            for entity in sorted(component.entities, key=lambda item: item.entity_id):
                if selected and entity.entity_id != selected:
                    continue
                state = self.hass.states.get(entity.entity_id)
                rows.append({"entity_id": entity.entity_id, "state": state.state if state else None,
                             "definition": entity.raw_config, "source": "loaded_raw_config"})
            return self.page(context, operation, args, rows, redactor, source="loaded_definition_cache")
        if operation == "ha_get_integration_config":
            selected = args.get("entry_id")
            rows = []
            for entry in sorted(self.hass.config_entries.async_entries(), key=lambda value: value.entry_id):
                if selected and selected != entry.entry_id:
                    continue
                rows.append({"entry_id": entry.entry_id, "domain": entry.domain, "title": entry.title,
                    "state": entry.state, "version": entry.version, "minor_version": getattr(entry, "minor_version", None),
                    "disabled_by": entry.disabled_by, "data": dict(entry.data), "options": dict(entry.options)})
            return self.page(context, operation, args, rows, redactor, source="config_entry_cache",
                             diagnostics_callbacks="excluded_unless_individually_audited")
        if operation == "ha_read_configuration":
            path = args.get("path", "configuration.yaml")
            value, revision = await self.execute(self.files.configuration, path)
            return self.page(context, operation, args, [{"path": path, "configuration": value}], redactor,
                             source="managed_yaml_syntax", file_revision=revision, templates_evaluated=False, includes_resolved=False)
        if operation == "ha_get_dashboard":
            path = args.get("url_path")
            if path is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", path):
                raise ReadError("invalid_query")
            module = self.modules("homeassistant.components.lovelace")
            data = self.hass.data.get(module.LOVELACE_DATA)
            dashboard = getattr(data, "dashboards", {}).get(path)
            if dashboard is None:
                return unavailable("dashboard_not_found")
            # Prefer the loaded primitive storage cache. No async_load callback:
            # YAML loaders resolve includes/secrets and can register watches.
            value = getattr(dashboard, "_data", None)
            if type(value) is dict and "config" in value:
                value = value["config"]
                revision = {"cache": True}
            elif type(getattr(dashboard, "config", None)) is dict and dashboard.config.get("filename"):
                value, revision = await self.execute(self.files.configuration, dashboard.config["filename"])
            else:
                value, revision = await self.execute(self.files.storage, "lovelace" + ("." + path if path else ""))
                value = value.get("config")
            return self.page(context, operation, args, [{"url_path": path, "configuration": value}], redactor,
                             source="dashboard_cache_or_managed_file", file_revision=revision)
        if operation == "ha_get_trace":
            return await self.trace(context, args, redactor)
        if operation == "ha_get_history":
            start, end = self.window(args)
            entity = self.entity_ids(args, single=True)[0]
            module = self.modules("homeassistant.components.recorder.history")
            def query():
                return module.state_changes_during_period(self.hass, start, end, entity_id=entity,
                    no_attributes=False, limit=MAX_ROWS + 1, include_start_time_state=True)
            values = await self.execute(query)
            states = values.get(entity, [])
            rows = [state_record(value) for value in states[:MAX_ROWS]]
            return self.page(context, operation, args, rows, redactor, source="ha_recorder_read_only",
                requested_range=[start.isoformat(), end.isoformat()], source_rows_limited=len(states) > MAX_ROWS,
                coverage="narrow_window_required" if len(states) > MAX_ROWS else "recorder_returned_rows",
                initial_state_semantics="HA may synthesize the opening state at the requested start",
                retention="Recorder exclusions and purge policy apply; empty does not prove no activity")
        if operation == "ha_get_statistics":
            start, end = self.window(args, max_seconds=366 * 86400)
            ids = args.get("statistic_ids")
            period = args.get("period", "hour")
            seconds = {"5minute": 300, "hour": 3600, "day": 86400, "week": 604800, "month": 28 * 86400}[period]
            if not ids or len(ids) > 16 or len(set(ids)) != len(ids) or (end - start).total_seconds() / seconds > MAX_ROWS:
                raise ReadError("invalid_query")
            module = self.modules("homeassistant.components.recorder.statistics")
            values = await self.execute(module.statistics_during_period, self.hass, start, end, set(ids), period, None,
                                        {"mean", "min", "max", "state", "sum", "last_reset", "change"})
            rows = [{"statistic_id": key, **value} for key in sorted(values) for value in values[key]]
            return self.page(context, operation, args, rows, redactor, source="ha_statistics_read_only",
                             period=period, requested_range=[start.isoformat(), end.isoformat()], retention="HA statistics retention applies")
        if operation == "ha_get_logbook":
            start, end = self.window(args, max_seconds=3600)
            ids = self.entity_ids(args) if args.get("entity_ids") else None
            values = await self.execute(self.logbook, start, end)
            limited = len(values) > MAX_ROWS
            rows = values[:MAX_ROWS]
            if ids:
                def matches(value):
                    if type(value) is not dict:
                        return False
                    targets = value.get("entity_id", value.get("entity_ids", []))
                    if type(targets) is str:
                        targets = [targets]
                    nested = value.get("target")
                    return type(targets) is list and bool(set(ids) & {v for v in targets if type(v) is str}) or matches(nested)
                rows = [row for row in rows if matches(row["data"])]
            return self.page(context, operation, args, rows, redactor, source="raw_recorder_events_read_only",
                requested_range=[start.isoformat(), end.isoformat()], source_rows_limited=limited,
                coverage="narrow_window_required" if limited else "stored_events_only",
                humanification="excluded: third-party logbook callbacks are not executed")
        if operation == "ha_get_logs":
            source = args.get("source", "core")
            if source == "system":
                handler = self.hass.data.get("system_log")
                if handler is None:
                    return unavailable("system_log_cache_unavailable")
                return self.page(context, operation, args, handler.records.to_list(), redactor,
                                 source="ha_system_log_cache", coverage="retained_deduplicated_errors")
            raw, revision = await self.execute(lambda: self.files.read("home-assistant.log", tail=True))
            text = redactor.text(raw.decode("utf-8", errors="replace"))
            lines = text.splitlines()
            if revision["tail_truncated"] and lines:
                lines = lines[1:]  # Do not disclose a partial leading line.
            if source == "power_sync":
                lines = [line for line in lines if "power_sync" in line]
            rows = [{"line": line[:16000], "line_truncated": len(line) > 16000} for line in lines]
            return self.page(context, operation, args, rows, redactor, source="ha_core_log_file", requested_source=source,
                             file_revision=revision, coverage="bounded_log_tail")
        if operation == "ha_get_system":
            value = self.hass.config.as_dict()
            return self.page(context, operation, args, [{"core": value, "python": platform.python_version(),
                "os": platform.system(), "machine": platform.machine(), "api_baseline": API_BASELINE,
                "installation_type": "supervised" if "hassio" in self.hass.config.components else "core_or_container"}],
                redactor, source="core_configuration_cache")
        if operation == "ha_get_supervisor":
            section = args.get("section", "info")
            module = self.modules("homeassistant.components.hassio")
            if section == "backup_metadata":
                backup = self.modules("homeassistant.components.backup")
                manager = backup.async_get_manager(self.hass)
                value = manager.known_backups.to_list()
            else:
                getter = {"info": module.get_info, "host": module.get_host_info, "addons": module.get_addons_list}[section]
                value = getter(self.hass)  # Audited cached coordinator getters only.
            rows = value if type(value) is list else [value]
            return self.page(context, operation, args, rows, redactor, source="supervisor_cache", section=section,
                             refresh_performed=False, backup_contents_included=False)
        raise ReadError("unsupported_reader")

    async def trace(self, context, args, redactor):
        domain, item = args.get("domain", "automation"), args.get("item_id")
        run = args.get("run_id")
        const = self.modules("homeassistant.components.trace.const")
        cached = self.hass.data.get(const.DATA_TRACE, {})
        rows = []
        for key, bucket in sorted(cached.items()):
            if not key.startswith(domain + ".") or item and key != domain + "." + item:
                continue
            values = bucket.all_traces() if hasattr(bucket, "all_traces") else bucket.values()
            for trace in values:
                if run and trace.run_id != run:
                    continue
                # HA's trace model builds a dictionary from stored trace data.
                # Never call the restore helper, which mutates the trace cache.
                rows.append({"item_id": key, "run_id": trace.run_id, "trace": trace.as_extended_dict()})
        try:
            stored_traces, _ = await self.execute(self.files.storage, "trace.saved_traces")
        except ReadError as error:
            if str(error) != "reader_unavailable":
                raise
            stored_traces = {}
        live = {(value["item_id"], value["run_id"]) for value in rows}
        for key, traces in stored_traces.items():
            if not key.startswith(domain + ".") or item and key != domain + "." + item:
                continue
            for trace in traces:
                if type(trace) is not dict or run and trace.get("run_id") != run or (key, trace.get("run_id")) in live:
                    continue
                rows.append({"item_id": key, "run_id": trace.get("run_id"), "trace": trace})
        return self.page(context, "ha_get_trace", args, rows, redactor, source="trace_cache_and_saved_syntax",
                         coverage="retained_traces_only", execution_performed=False)

    def logbook(self, start, end):
        """Fixed bounded SELECT, with no third-party humanification callbacks."""
        from sqlalchemy import func, select
        schema = self.modules("homeassistant.components.recorder.db_schema")
        session_scope = self.modules("homeassistant.helpers.recorder").session_scope
        events, kinds, data = schema.Events, schema.EventTypes, schema.EventData
        statement = (select(events.event_id, events.time_fired_ts, kinds.event_type, func.substr(data.shared_data, 1, 65537))
            .join(kinds, events.event_type_id == kinds.event_type_id)
            .outerjoin(data, events.data_id == data.data_id)
            .where(events.time_fired_ts >= start.timestamp(), events.time_fired_ts < end.timestamp())
            .order_by(events.time_fired_ts, events.event_id).limit(MAX_ROWS + 1))
        result = []
        with session_scope(hass=self.hass, read_only=True) as session:
            for identity, timestamp, kind, raw in session.execute(statement):
                value = None
                if raw and len(raw) <= 65536:
                    try:
                        value = json.loads(raw)
                    except (ValueError, TypeError, RecursionError):
                        value = {"unavailable": True, "reason": "invalid_stored_event"}
                elif raw:
                    value = {"unavailable": True, "reason": "source_data_limit"}
                result.append({"event_id": identity, "timestamp": timestamp, "event_type": kind, "data": value})
        return result


def create_backend(hass, *, version=None, modules=import_module, wall=None):
    backend = HAReadBackend(hass, **({"wall": wall} if wall else {}))
    readers = LocalReaders(backend, version=version, modules=modules)
    backend.projector = lambda value: readers.redactor().project(value)
    backend.adapters = {name: LocalReader(readers, name) for name in READERS}
    return backend
