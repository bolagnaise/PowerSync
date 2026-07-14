from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "powersync_quota_test", ROOT / "custom_components" / "power_sync" / "quota.py"
)
quota = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = quota
SPEC.loader.exec_module(quota)
QuotaLedger = quota.QuotaLedger
QuotaLedgerState = quota.QuotaLedgerState
QuotaRule = quota.QuotaRule
import_legacy_settled_state = quota.import_legacy_settled_state

AEST = timezone(timedelta(hours=10))


def _rules() -> tuple[QuotaRule, QuotaRule]:
    return (
        QuotaRule("free", "import", "AEST", (("11:00", "14:00"),), 50, 35.17, 35.17),
        QuotaRule("premium", "export", "AEST", (("18:00", "21:00"),), 30, 5, 10),
    )


def _authoritative_state() -> QuotaLedgerState:
    return QuotaLedgerState(
        tariff_day="2026-07-14",
        timezone_token="AEST",
        confidence="authoritative",
        settled_kwh={"free": 0.0, "premium": 0.0},
    )


def test_cumulative_readings_are_idempotent_and_cap_partial_interval() -> None:
    state = _authoritative_state()
    state.settled_kwh["free"] = 49.9
    ledger = QuotaLedger(_rules(), state)
    start = datetime(2026, 7, 14, 11, 0, tzinfo=AEST)

    assert ledger.observe_cumulative("import", 100.0, start) == 0
    assert ledger.observe_cumulative("import", 100.2, start + timedelta(minutes=5)) == pytest.approx(0.1)
    assert ledger.bucket("free").settled_kwh == 50
    assert ledger.bucket("free").remaining_kwh == 0
    assert ledger.observe_cumulative("import", 100.2, start + timedelta(minutes=5)) == 0
    assert ledger.observe_cumulative("import", 99.0, start - timedelta(minutes=5)) == 0
    assert ledger.bucket("free").settled_kwh == 50


def test_interval_energy_is_split_at_window_boundary() -> None:
    ledger = QuotaLedger(_rules(), _authoritative_state())
    start = datetime(2026, 7, 14, 10, 55, tzinfo=AEST)
    ledger.observe_cumulative("import", 10.0, start)
    settled = ledger.observe_cumulative("import", 12.0, start + timedelta(minutes=10))
    assert settled == pytest.approx(1.0)


def test_unchanged_poll_does_not_shift_a_delayed_delta_across_window_boundary() -> None:
    ledger = QuotaLedger(_rules(), _authoritative_state())
    start = datetime(2026, 7, 14, 10, 55, tzinfo=AEST)
    ledger.observe_cumulative("import", 10.0, start)
    ledger.observe_cumulative("import", 10.0, start + timedelta(minutes=4))

    settled = ledger.observe_cumulative(
        "import",
        12.0,
        start + timedelta(minutes=10),
    )

    assert settled == pytest.approx(1.0)


def test_power_gap_disables_bonus_until_next_tariff_day() -> None:
    ledger = QuotaLedger(_rules(), _authoritative_state(), continuity_seconds=600)
    start = datetime(2026, 7, 14, 11, 0, tzinfo=AEST)
    ledger.observe_power("import", 1000, start)
    ledger.observe_power("import", 1000, start + timedelta(minutes=11))
    assert ledger.state.confidence == "unknown"
    assert ledger.state.reason == "power telemetry gap"
    assert ledger.bucket("free").effective_price_c_per_kwh == pytest.approx(35.17)


def test_midday_first_setup_is_unknown_but_reset_baselines_restore_confidence() -> None:
    ledger = QuotaLedger(_rules())
    midday = datetime(2026, 7, 14, 12, 0, tzinfo=AEST)
    ledger.observe_cumulative("import", 10, midday)
    ledger.observe_cumulative("export", 20, midday)
    assert ledger.state.confidence == "unknown"

    reset = datetime(2026, 7, 15, 0, 1, tzinfo=AEST)
    ledger.observe_cumulative("import", 11, reset)
    ledger.observe_cumulative("export", 21, reset)
    assert ledger.state.confidence == "authoritative"


def test_mixed_meter_and_power_sources_are_never_authoritative() -> None:
    ledger = QuotaLedger(_rules())
    reset = datetime(2026, 7, 14, 0, 1, tzinfo=AEST)

    ledger.observe_cumulative("import", 100.0, reset)
    ledger.observe_power("export", 0.0, reset)

    assert ledger.state.confidence == "estimated"
    assert ledger.state.settled_kwh == {"free": 0.0, "premium": 0.0}


def test_legacy_import_is_idempotent() -> None:
    state = QuotaLedgerState(settled_kwh={})
    import_legacy_settled_state(state, {"bonus_export_kwh": 7.5}, {"legacy": "bonus_export_kwh"})
    import_legacy_settled_state(state, {"bonus_export_kwh": 9.0}, {"legacy": "bonus_export_kwh"})
    assert state.settled_kwh["legacy"] == 7.5
