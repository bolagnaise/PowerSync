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


def test_stale_present_reads_before_field_absence_still_tolerate_enable():
    """The reported ticket-61 variant: a stale readback precedes the omission.

    Tesla omits ``disallow_charge_from_grid_with_solar_installed`` from
    ``site_info`` while grid charging is allowed, so an enable can never read
    back its own desired value.  When the first reads still carry the previous
    (disallowed) state and the later reads omit the field, the omission is the
    newest evidence and describes the requested state.
    """
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info(False),
            _site_info(False),
            _site_info_without_grid_charging_field(),
            _site_info_without_grid_charging_field(),
        ],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_FIELD_ABSENT
    )
    assert len(session.get_calls) == 4


def test_field_absent_warning_reports_how_many_reads_omitted_the_field(caplog):
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info(False),
            _site_info(False),
            _site_info_without_grid_charging_field(),
            _site_info_without_grid_charging_field(),
        ],
    )

    with caplog.at_level("WARNING"):
        asyncio.run(_set(session, clock))

    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "the newest direct site_info readback omitted" in message
    assert "2 of 4 valid readback(s) omitted it" in message


def test_disable_keeps_the_unanimous_field_absence_rule():
    """The relaxed ordering rule is deliberately enable-only.

    An omission does not describe a requested *disable*, so this direction keeps
    the stricter unanimous rule: a site that never exposes the field at all must
    still be able to complete a restore instead of being left force-active.
    """
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info_without_grid_charging_field() for _ in range(4)],
    )

    outcome = asyncio.run(_set(session, clock, enabled=False))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_FIELD_ABSENT
    )


def test_disable_after_a_present_read_is_not_tolerated_by_ordering():
    """Guard: the enable-only relaxation must not leak into the disable path."""
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info(True),
            _site_info(True),
            _site_info_without_grid_charging_field(),
            _site_info_without_grid_charging_field(),
        ],
    )

    outcome = asyncio.run(_set(session, clock, enabled=False))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )
    assert not outcome.applied


def test_present_field_still_confirms_a_disable():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info(False)],
    )

    outcome = asyncio.run(_set(session, clock, enabled=False))

    assert outcome.status is tesla_grid_control.TeslaGridWriteStatus.APPLIED


def test_stale_present_read_after_field_absence_stays_unconfirmed_for_enable():
    """A contradicting read that arrives last is not overridden by an earlier omission."""
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info_without_grid_charging_field(),
            _site_info_without_grid_charging_field(),
            _site_info(False),
            _site_info(False),
        ],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_unconfirmed_warning_reports_the_readback_census(caplog):
    """The failure line must distinguish its sub-causes for support captures."""
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[_site_info(False) for _ in range(4)],
    )

    with caplog.at_level("WARNING"):
        outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )
    message = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    )
    assert "4 valid readback(s)" in message
    assert "0 omitted the field" in message
    assert "last observed enabled=False" in message


def _thin_site_info() -> _Response:
    """An HTTP 200 that carries a top-level marker but no components block."""
    return _Response(200, {"response": {"site_name": "Home"}})


def test_thin_payload_cannot_override_an_explicit_contrary_readback():
    """A response that never reported components is not evidence of omission.

    ``{"site_name": ...}`` satisfies tesla_site_info_has_structure through a
    top-level marker alone.  Treating its missing grid-charging field as an
    omission would let one truncated response outweigh the only readback that
    actually carried grid information.
    """
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info(False),
            _site_info(False),
            _site_info(False),
            _thin_site_info(),
        ],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_thin_payload_after_a_real_omission_does_not_grant_tolerance():
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info(False),
            _site_info(False),
            _site_info_without_grid_charging_field(),
            _thin_site_info(),
        ],
    )

    outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )


def test_components_reported_probe_separates_thin_from_real_payloads():
    assert not tesla_grid_control.tesla_site_info_reports_components(
        {"site_name": "Home"}
    )
    assert not tesla_grid_control.tesla_site_info_reports_components(
        {"components": {"unrelated": True}}
    )
    assert tesla_grid_control.tesla_site_info_reports_components(
        {"components": {"grid_status": "SystemGridConnected"}}
    )


def test_last_observed_is_not_reset_by_a_structurally_invalid_payload(caplog):
    clock = _Clock()
    session = _Session(
        posts=[_Response(200, {"response": {"result": True}})],
        gets=[
            _site_info_without_grid_charging_field(),
            _site_info_without_grid_charging_field(),
            _site_info(False),
            _Response(200, {"response": {}}),
        ],
    )

    with caplog.at_level("WARNING"):
        outcome = asyncio.run(_set(session, clock))

    assert (
        outcome.status
        is tesla_grid_control.TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
    )
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "last observed enabled=False" in message
    assert "invalid readback=True" in message
