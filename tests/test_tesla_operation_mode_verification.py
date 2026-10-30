"""Behavioral regression tests for Tesla operation-mode verification."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
GRID_CONTROL_PATH = (
    ROOT / "custom_components" / "power_sync" / "tesla_grid_control.py"
)


def _nested_cloud_source() -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if not (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_setup_entry"
        ):
            continue
        for child in node.body:
            if not (
                isinstance(child, ast.AsyncFunctionDef)
                and child.name == "handle_set_operation_mode"
            ):
                continue
            for nested in child.body:
                if (
                    isinstance(nested, ast.AsyncFunctionDef)
                    and nested.name == "_cloud"
                ):
                    segment = ast.get_source_segment(source, nested)
                    assert segment is not None
                    return segment
    raise AssertionError("handle_set_operation_mode._cloud not found")


def _load_site_info_structure_helper():
    spec = importlib.util.spec_from_file_location(
        "tesla_grid_control_for_operation_mode_test",
        GRID_CONTROL_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.tesla_site_info_has_structure


class _Response:
    def __init__(
        self,
        status: int,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload

    async def text(self) -> str:
        return self._text


class _Session:
    def __init__(
        self,
        *,
        posts: list[_Response],
        gets: list[_Response],
    ) -> None:
        self.posts = posts
        self.gets = gets
        self.post_calls = 0
        self.get_calls = 0

    def post(self, *_args, **_kwargs):
        self.post_calls += 1
        return self.posts.pop(0)

    def get(self, *_args, **_kwargs):
        self.get_calls += 1
        return self.gets.pop(0)


class _Logger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


async def _run_cloud(
    readbacks: list[_Response],
    *,
    mode: str = "self_consumption",
) -> tuple[bool, _Session, list[str]]:
    session = _Session(
        posts=[
            _Response(200, text='{"response":{"result":true}}')
            for _ in range(3 if mode == "autonomous" else 1)
        ],
        gets=readbacks,
    )
    notifications: list[str] = []

    async def _sleep(_seconds: float) -> None:
        return None

    async def _notify_api_error(_hass, title: str, _message: str) -> None:
        notifications.append(title)

    hass = SimpleNamespace(data={"power_sync": {"entry-1": {}}})
    tasks = []

    def _create_task(coro):
        tasks.append(asyncio.create_task(coro))

    hass.async_create_task = _create_task
    entry = SimpleNamespace(data={}, entry_id="entry-1")
    namespace = {
        "CONF_FLEET_API_BASE_URL": "fleet_api_base_url",
        "DOMAIN": "power_sync",
        # handle_set_operation_mode's generation guard; this request stays current.
        "current": lambda: True,
        "_LOGGER": _Logger(),
        "_get_tesla_site_configs": lambda *_args: [
            ("site-1", "token", "fleet")
        ],
        "_notify_api_error": _notify_api_error,
        "aiohttp": aiohttp,
        "async_get_clientsession": lambda _hass: session,
        "asyncio": SimpleNamespace(
            sleep=_sleep,
            TimeoutError=asyncio.TimeoutError,
            CancelledError=asyncio.CancelledError,
        ),
        "entry": entry,
        "get_tesla_api_base_url": lambda *_args: "https://fleet.example",
        "hass": hass,
        "mode": mode,
        "tesla_site_info_has_structure": _load_site_info_structure_helper(),
    }
    exec(compile(_nested_cloud_source(), "<operation_mode_cloud>", "exec"), namespace)
    success = await namespace["_cloud"]()
    if tasks:
        await asyncio.gather(*tasks)
    return success, session, notifications


def _structured_site_info_without_mode() -> _Response:
    return _Response(
        200,
        {
            "response": {
                "site_name": "Home",
                "components": {"grid_status": "SystemGridConnected"},
            }
        },
    )


def _structured_site_info_with_mode(mode: str) -> _Response:
    return _Response(
        200,
        {
            "response": {
                "site_name": "Home",
                "default_real_mode": mode,
            }
        },
    )


def test_accepted_mode_write_with_repeated_valid_field_absence_is_compatible():
    success, session, notifications = asyncio.run(
        _run_cloud(
            [_structured_site_info_without_mode() for _ in range(4)]
        )
    )

    assert success is True
    assert session.post_calls == 1
    assert session.get_calls == 4
    assert notifications == []


def test_accepted_mode_write_with_explicit_conflicting_readback_still_fails():
    success, session, notifications = asyncio.run(
        _run_cloud(
            [_structured_site_info_with_mode("autonomous") for _ in range(4)]
        )
    )

    assert success is False
    assert session.post_calls == 1
    assert session.get_calls == 4
    assert notifications == ["Mode Change Failed"]


def test_mode_field_absence_does_not_bypass_autonomous_recovery():
    success, session, notifications = asyncio.run(
        _run_cloud(
            [_structured_site_info_without_mode() for _ in range(8)],
            mode="autonomous",
        )
    )

    assert success is False
    assert session.post_calls == 3
    assert session.get_calls == 8
    assert notifications == ["Mode Change Failed"]


def test_one_valid_absence_amid_read_errors_stays_unconfirmed():
    success, session, notifications = asyncio.run(
        _run_cloud(
            [
                _structured_site_info_without_mode(),
                _Response(503, text="unavailable"),
                _Response(503, text="unavailable"),
                _Response(503, text="unavailable"),
            ]
        )
    )

    assert success is False
    assert session.post_calls == 1
    assert session.get_calls == 4
    assert notifications == ["Mode Change Failed"]


def test_mixed_absence_and_explicit_mismatch_stays_unconfirmed():
    success, session, notifications = asyncio.run(
        _run_cloud(
            [
                _structured_site_info_without_mode(),
                _structured_site_info_with_mode("autonomous"),
                _structured_site_info_without_mode(),
                _structured_site_info_without_mode(),
            ]
        )
    )

    assert success is False
    assert session.post_calls == 1
    assert session.get_calls == 4
    assert notifications == ["Mode Change Failed"]


def test_direct_site_info_expected_mode_is_confirmed():
    success, session, notifications = asyncio.run(
        _run_cloud(
            [
                _Response(
                    200,
                    {
                        "site_name": "Home",
                        "default_real_mode": "self_consumption",
                    },
                )
            ]
        )
    )

    assert success is True
    assert session.post_calls == 1
    assert session.get_calls == 1
    assert notifications == []
