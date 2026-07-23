"""Focused coverage for atomic automation-group renames."""

from __future__ import annotations

import ast
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


ROOT = Path(__file__).resolve().parent.parent
AUTOMATIONS_PATH = ROOT / "custom_components" / "power_sync" / "automations" / "__init__.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _load_class(path: Path, class_name: str, namespace: dict[str, Any]):
    tree = ast.parse(path.read_text())
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    module = ast.fix_missing_locations(ast.Module(body=[class_node], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[class_name]


def _automation_store(automations: list[dict[str, Any]]):
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "HomeAssistant": object,
        "List": List,
        "Optional": Optional,
        "Store": object,
        "datetime": datetime,
        "timezone": timezone,
    }
    store_class = _load_class(AUTOMATIONS_PATH, "AutomationStore", namespace)
    store = object.__new__(store_class)
    store._data = {"automations": automations, "next_id": len(automations) + 1}
    return store


def test_store_renames_every_exact_match_with_one_save():
    automations = [
        {"id": 1, "group_name": "global", "updated_at": "old-1"},
        {"id": 2, "group_name": "global", "updated_at": "old-2"},
        {"id": 3, "group_name": "Global", "updated_at": "untouched"},
    ]
    store = _automation_store(automations)
    save_calls = 0

    async def async_save():
        nonlocal save_calls
        save_calls += 1

    store.async_save = async_save

    updated_count = asyncio.run(store.async_rename_group("global", "  House  "))

    assert updated_count == 2
    assert save_calls == 1
    assert [automation["group_name"] for automation in automations] == [
        "House",
        "House",
        "Global",
    ]
    assert automations[0]["updated_at"] == automations[1]["updated_at"]
    assert automations[0]["updated_at"] not in {"old-1", "old-2"}
    assert automations[2]["updated_at"] == "untouched"


def test_store_rename_merges_into_existing_group():
    automations = [
        {"id": 1, "group_name": "Old", "updated_at": "old"},
        {"id": 2, "group_name": "Existing", "updated_at": "existing"},
    ]
    store = _automation_store(automations)
    save_calls = 0

    async def async_save():
        nonlocal save_calls
        save_calls += 1

    store.async_save = async_save

    assert asyncio.run(store.async_rename_group("Old", "Existing")) == 1
    assert save_calls == 1
    assert [automation["group_name"] for automation in automations] == [
        "Existing",
        "Existing",
    ]
    assert store.get_groups() == ["Default Group", "Existing"]


@pytest.mark.parametrize(
    ("old_name", "new_name", "message"),
    [
        (None, "Target", "old_name must be a non-empty string"),
        ("Old", "   ", "new_name must be a non-empty string"),
        ("Old", " Old ", "new_name must be different from old_name"),
    ],
)
def test_store_rejects_invalid_names(old_name, new_name, message):
    store = _automation_store([{"id": 1, "group_name": "Old"}])

    with pytest.raises(ValueError, match=message):
        asyncio.run(store.async_rename_group(old_name, new_name))


def test_store_returns_zero_without_saving_when_exact_group_is_not_found():
    store = _automation_store([{"id": 1, "group_name": "Global"}])
    save_calls = 0

    async def async_save():
        nonlocal save_calls
        save_calls += 1

    store.async_save = async_save

    assert asyncio.run(store.async_rename_group("global", "House")) == 0
    assert save_calls == 0
    assert store.get_all()[0]["group_name"] == "Global"


def test_store_rolls_back_group_and_timestamp_when_save_fails():
    automations = [
        {"id": 1, "group_name": "Old", "updated_at": "before"},
        {"id": 2, "group_name": "Old"},
    ]
    store = _automation_store(automations)

    async def async_save():
        raise RuntimeError("disk unavailable")

    store.async_save = async_save

    with pytest.raises(RuntimeError, match="disk unavailable"):
        asyncio.run(store.async_rename_group("Old", "New"))

    assert automations == [
        {"id": 1, "group_name": "Old", "updated_at": "before"},
        {"id": 2, "group_name": "Old"},
    ]


class _Response:
    def __init__(self, body: dict[str, Any], status: int = 200):
        self.body = body
        self.status = status


class _Request:
    def __init__(self, body: Any):
        self._body = body

    async def json(self):
        return self._body


def _rename_view(store):
    web = SimpleNamespace(
        Request=object,
        Response=object,
        json_response=lambda body, status=200: _Response(body, status),
    )
    namespace = {
        "DOMAIN": "power_sync",
        "HomeAssistant": object,
        "HomeAssistantView": object,
        "_LOGGER": logging.getLogger(__name__),
        "web": web,
    }
    view_class = _load_class(INIT_PATH, "AutomationGroupRenameView", namespace)
    hass = SimpleNamespace(data={"power_sync": {"automation_store": store}})
    return view_class(hass)


def test_rename_endpoint_returns_canonical_names_and_updated_count():
    store = _automation_store([
        {"id": 1, "group_name": "global"},
        {"id": 2, "group_name": "global"},
    ])

    async def async_save():
        return None

    store.async_save = async_save
    view = _rename_view(store)

    response = asyncio.run(view.post(_Request({
        "old_name": "global",
        "new_name": "  House  ",
    })))

    assert view.url == "/api/power_sync/automations/groups/rename"
    assert view.requires_auth is True
    assert response.status == 200
    assert response.body == {
        "success": True,
        "old_name": "global",
        "new_name": "House",
        "updated_count": 2,
    }


def test_rename_endpoint_returns_clear_validation_and_not_found_errors():
    store = _automation_store([{"id": 1, "group_name": "Global"}])

    async def async_save():
        return None

    store.async_save = async_save
    view = _rename_view(store)

    invalid = asyncio.run(view.post(_Request({"old_name": "Global", "new_name": " "})))
    missing = asyncio.run(view.post(_Request({"old_name": "global", "new_name": "House"})))

    assert invalid.status == 400
    assert invalid.body == {
        "success": False,
        "error": "new_name must be a non-empty string",
    }
    assert missing.status == 404
    assert missing.body == {
        "success": False,
        "error": "Automation group not found",
    }


def test_rename_view_is_registered():
    source = INIT_PATH.read_text()

    assert "hass.http.register_view(AutomationGroupRenameView(hass))" in source
