"""Behavioral tests for confirmed Tesla grid-charging writes."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "power_sync"
    / "tesla_grid_control.py"
)
SPEC = importlib.util.spec_from_file_location("tesla_grid_control_test", MODULE_PATH)
assert SPEC and SPEC.loader
tesla_grid_control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tesla_grid_control)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Response:
    def __init__(
        self,
        status: int,
        payload: dict[str, Any] | None = None,
        *,
        text: str = "",
        headers: dict[str, str] | None = None,
        on_enter=None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}
        self._on_enter = on_enter

    async def __aenter__(self):
        if self._on_enter is not None:
            self._on_enter()
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
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self.posts.pop(0)

    def get(self, url: str, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self.gets.pop(0)


def _site_info(enabled: bool) -> _Response:
    return _Response(
        200,
        {
            "response": {
                "components": {
                    "disallow_charge_from_grid_with_solar_installed": not enabled
                }
            }
        },
    )


def _site_info_without_grid_charging_field() -> _Response:
    return _Response(
        200,
        {
            "response": {
                "components": {
                    "grid_status": "SystemGridConnected",
                },
                "default_real_mode": "autonomous",
            }
        },
    )


async def _set(
    session: _Session,
    clock: _Clock,
    *,
    enabled: bool = True,
    is_current=None,
):
    return await tesla_grid_control.async_set_tesla_grid_charging_confirmed(
        session,
        "https://fleet-api.prd.na.vn.cloud.tesla.com",
        "site-1",
        {"Authorization": "Bearer token"},
        enabled,
        confirmation_deadline=4.0,
        poll_offsets=(0.0, 1.0, 2.0, 3.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        is_current=is_current,
    )


def test_accepted_write_polls_only_until_eventual_readback_confirmation():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_Response(503), _site_info(False), _site_info(True)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert outcome.status is tesla_grid_control.TeslaGridWriteStatus.APPLIED
    assert len(session.post_calls) == 1
    assert len(session.get_calls) == 3
    assert session.post_calls[0]["json"] == {
        "disallow_charge_from_grid_with_solar_installed": False
    }


def test_accepted_write_that_never_applies_is_not_reported_successful():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info(False) for _ in range(4)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )
    assert len(session.post_calls) == 1


def test_accepted_write_with_repeated_field_absence_is_classified_separately():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info_without_grid_charging_field() for _ in range(4)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_FIELD_ABSENT
    )
    assert not outcome.applied
    assert len(session.get_calls) == 4


def test_empty_or_non_site_info_does_not_become_field_absent_compatibility():
    for payload in (
        {"response": {}},
        {"response": {"unrelated": True}},
        {"response": {"components": {"unrelated": True}}},
    ):
        clock = _Clock()
        session = _Session(
            posts=[_Response(200, {"response": {"result": True}})],
            gets=[_Response(200, payload) for _ in range(4)],
        )

        outcome = asyncio.run(_set(session, clock))

        assert (
            outcome.status
            is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
        )


def test_field_absence_followed_by_desired_readback_is_applied():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info_without_grid_charging_field(), _site_info(True)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert outcome.status is tesla_grid_control.TeslaGridWriteStatus.APPLIED


def test_field_absence_followed_by_opposite_readback_stays_unconfirmed():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info_without_grid_charging_field(),
            _site_info(False),
            _site_info(False),
            _site_info(False),
        ],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_present_but_invalid_field_stays_unconfirmed():
    clock = _Clock()
    invalid = _Response(
        200,
        {
            "response": {
                "components": {
                    "disallow_charge_from_grid_with_solar_installed": None
                }
            }
        },
    )
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[invalid, invalid, invalid, invalid],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_no_parseable_site_info_stays_unconfirmed():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_Response(200) for _ in range(4)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_single_field_absent_read_amid_transport_failures_stays_unconfirmed():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info_without_grid_charging_field(),
            _Response(503),
            _Response(503),
            _Response(503),
        ],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_transient_post_is_retried_but_accepted_post_is_not_duplicated():
    clock = _Clock()
    session = _Session(
        posts=[
            _Response(503, text="upstream unavailable"),
            _Response(200, {"response": {"result": True}}),
        ],
        gets=[_site_info(True)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert outcome.status is tesla_grid_control.TeslaGridWriteStatus.APPLIED
    assert len(session.post_calls) == 2
    assert len(session.get_calls) == 1


def test_slow_read_skips_missed_absolute_poll_slots():
    clock = _Clock()
    slow_stale = _Response(
        200,
        {
            "response": {
                "disallow_charge_from_grid_with_solar_installed": True
            }
        },
        on_enter=lambda: setattr(clock, "now", clock.now + 2.6),
    )
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[slow_stale, _site_info(True)],
    )

    outcome = asyncio.run(_set(session, clock))

    assert outcome.status is tesla_grid_control.TeslaGridWriteStatus.APPLIED
    assert len(session.get_calls) == 2
    assert abs(clock.now - 3.0) < 1e-9


def test_response_body_rejection_is_rejected_without_readback():
    clock = _Clock()
    session = _Session(
        posts=[
            _Response(
                200,
                {"response": {"result": False, "reason": "not_allowed"}},
            )
        ],
        gets=[],
    )

    outcome = asyncio.run(_set(session, clock))

    assert outcome.status is tesla_grid_control.TeslaGridWriteStatus.REJECTED
    assert outcome.detail == "not_allowed"
    assert not session.get_calls


def test_generation_change_aborts_confirmation_without_another_write():
    clock = _Clock()
    current = True

    async def sleep_and_supersede(seconds: float) -> None:
        nonlocal current
        await clock.sleep(seconds)
        current = False

    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info(False)],
    )

    outcome = asyncio.run(
        tesla_grid_control.async_set_tesla_grid_charging_confirmed(
            session,
            "https://fleet-api.prd.na.vn.cloud.tesla.com",
            "site-1",
            {"Authorization": "Bearer token"},
            True,
            confirmation_deadline=4.0,
            poll_offsets=(0.0, 1.0, 2.0),
            sleep=sleep_and_supersede,
            monotonic=clock.monotonic,
            is_current=lambda: current,
        )
    )

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )
    assert outcome.detail == "superseded"
    assert len(session.post_calls) == 1
    assert len(session.get_calls) == 1


def test_generation_change_after_field_absence_is_not_compatibility_success():
    clock = _Clock()
    current = True

    async def sleep_and_supersede(seconds: float) -> None:
        nonlocal current
        await clock.sleep(seconds)
        current = False

    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info_without_grid_charging_field()],
    )

    outcome = asyncio.run(
        tesla_grid_control.async_set_tesla_grid_charging_confirmed(
            session,
            "https://fleet-api.prd.na.vn.cloud.tesla.com",
            "site-1",
            {"Authorization": "Bearer token"},
            True,
            confirmation_deadline=4.0,
            poll_offsets=(0.0, 1.0, 2.0),
            sleep=sleep_and_supersede,
            monotonic=clock.monotonic,
            is_current=lambda: current,
        )
    )

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )
    assert outcome.detail == "superseded"


def test_grid_charging_state_extraction_accepts_nested_and_top_level_shapes():
    extract = tesla_grid_control.tesla_grid_charging_enabled_from_site_info

    assert extract(
        {
            "components": {
                "disallow_charge_from_grid_with_solar_installed": "false"
            }
        }
    ) is True
    assert extract({"disallow_charge_from_grid_with_solar_installed": True}) is False
    assert extract({}) is None


def test_grid_charging_field_presence_distinguishes_absent_from_invalid():
    present = tesla_grid_control.tesla_grid_charging_field_present

    assert present(
        {
            "components": {
                "disallow_charge_from_grid_with_solar_installed": None
            }
        }
    )
    assert present(
        {"disallow_charge_from_grid_with_solar_installed": "unsupported"}
    )
    assert not present({"components": {"grid_status": "SystemGridConnected"}})
