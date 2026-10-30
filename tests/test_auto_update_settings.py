"""Dashboard schedule edits retain unrelated options and target the right entry."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _view():
    namespace = {
        "HomeAssistantView": object,
        "CONF_AUTO_UPDATE_TIME": "auto_update_time",
        "DEFAULT_AUTO_UPDATE_TIME": "03:00",
        "DOMAIN": "power_sync",
        "Any": object,
        "web": SimpleNamespace(json_response=lambda body, status=200: SimpleNamespace(body=body, status=status)),
        "async_dispatcher_send": lambda *args: None,
    }
    for filename, names in (
        ("auto_update.py", {"parse_auto_update_time", "normalize_auto_update_time"}),
        ("auto_update_settings.py", {"AutoUpdateSettingsView"}),
    ):
        tree = ast.parse((ROOT / filename).read_text())
        tree.body = [node for node in tree.body if getattr(node, "name", None) in names]
        exec(compile(tree, filename, "exec"), namespace)
    entry = SimpleNamespace(
        entry_id="target", domain="power_sync", data={},
        options={"auto_update_time": "03:00", "auto_update_enabled": True, "unrelated": "keep"},
    )
    other = SimpleNamespace(entry_id="other", options={"auto_update_time": "04:00"})
    entity = SimpleNamespace(platform="power_sync", config_entry_id="target")
    registry = SimpleNamespace(async_get=lambda entity_id: entity if entity_id == "switch.renamed_update" else None)
    namespace["er"] = SimpleNamespace(async_get=lambda hass: registry)
    entries = {"target": entry, "other": other}
    updates = []

    def update(target, *, options):
        updates.append(target.entry_id)
        target.options = options

    hass = SimpleNamespace(data={"power_sync": {"target": {}}}, config_entries=SimpleNamespace(async_get_entry=entries.get, async_update_entry=update))
    return namespace["AutoUpdateSettingsView"](hass), entry, other, updates


class Request(dict):
    def __init__(self, payload, *, admin=True, entity_id="switch.renamed_update"):
        super().__init__(hass_user=SimpleNamespace(is_admin=admin))
        self.query = {"entity_id": entity_id}
        self.payload = payload

    async def json(self):
        return self.payload


def test_schedule_update_targets_renamed_entity_and_preserves_options():
    view, entry, other, updates = _view()
    request = Request({"auto_update_time": "05:45:00"})
    response = asyncio.run(view.post(request))
    assert response.status == 200
    assert updates == ["target"]
    assert view._hass.data["power_sync"]["target"]["_skip_reload"] is True
    assert entry.options == {"auto_update_time": "05:45", "auto_update_enabled": True, "unrelated": "keep"}
    assert other.options == {"auto_update_time": "04:00"}
    readback = asyncio.run(view.get(request))
    assert readback.body["config"]["auto_update_time"] == "05:45"


def test_schedule_rejects_invalid_time_and_unrelated_writes():
    for payload in ({"auto_update_time": "25:00"}, {"auto_update_time": ""}, {}, [], {"auto_update_time": "05:00", "auto_update_enabled": False}):
        view, entry, _, updates = _view()
        response = asyncio.run(view.post(Request(payload)))
        assert response.status == 400
        assert updates == []
        assert entry.options["auto_update_time"] == "03:00"


def test_schedule_requires_admin_and_a_valid_owning_entry():
    for request, status in ((Request({"auto_update_time": "05:00"}, admin=False), 403), (Request({"auto_update_time": "05:00"}, entity_id="switch.unrelated"), 404)):
        view, _, _, updates = _view()
        assert asyncio.run(view.post(request)).status == status
        assert updates == []


def test_schedule_noop_does_not_set_a_reload_skip_flag():
    view, _, _, updates = _view()
    assert asyncio.run(view.post(Request({"auto_update_time": "03:00"}))).status == 200
    assert updates == []
    assert "_skip_reload" not in view._hass.data["power_sync"]["target"]
