"""Regression tests for evidence-backed solar forecast provenance."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import ast
from types import SimpleNamespace

import pytest


_MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components/power_sync/optimization/solar_provenance.py"
)
_SPEC = importlib.util.spec_from_file_location("solar_provenance", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
derive_solar_forecast_provenance = _MODULE.derive_solar_forecast_provenance
_COORDINATOR_SOURCE = (
    Path(__file__).parents[1]
    / "custom_components/power_sync/optimization/coordinator.py"
).read_text()


def test_combines_explicit_pre_lp_cap_and_lp_spillage_without_nowcast_gap():
    result = derive_solar_forecast_provenance(
        raw_weather_kw=[4.0, 4.0, 2.0],
        adjusted_available_kw=[4.0, 3.0, 2.0],
        solver_available_kw=[4.0, 2.0, 2.0],
        lp_curtailment_w=[0.0, 500.0, 0.0],
    )

    assert result is not None
    assert result.raw_forecast_values_kw == [4.0, 4.0, 2.0]
    assert result.planned_forecast_values_kw == [4.0, 1.5, 2.0]
    assert result.curtailment_values_kw == [0.0, 1.5, 0.0]
    assert [
        planned + curtailed
        for planned, curtailed in zip(
            result.planned_forecast_values_kw,
            result.curtailment_values_kw,
            strict=True,
        )
    ] == [4.0, 3.0, 2.0]


def test_nowcast_only_gap_is_not_curtailment():
    result = derive_solar_forecast_provenance(
        raw_weather_kw=[4.0, 4.0],
        adjusted_available_kw=[4.0, 3.0],
        solver_available_kw=[4.0, 3.0],
        lp_curtailment_w=[0.0, 0.0],
    )

    assert result is not None
    assert result.planned_forecast_values_kw == [4.0, 3.0]
    assert result.curtailment_values_kw == [0.0, 0.0]


def test_missing_misaligned_or_invalid_lp_evidence_fails_closed():
    common = {
        "raw_weather_kw": [4.0, 4.0],
        "adjusted_available_kw": [4.0, 3.0],
        "solver_available_kw": [4.0, 3.0],
    }
    assert derive_solar_forecast_provenance(**common, lp_curtailment_w=None) is None
    assert derive_solar_forecast_provenance(
        **common, lp_curtailment_w=[0.0]
    ) is None
    assert derive_solar_forecast_provenance(
        **common, lp_curtailment_w=[0.0, float("nan")]
    ) is None
    assert derive_solar_forecast_provenance(
        **common, lp_curtailment_w=[0.0, 4000.0]
    ) is None


def test_solver_noise_is_clamped_but_material_inconsistency_fails_closed():
    result = derive_solar_forecast_provenance(
        raw_weather_kw=[1.0],
        adjusted_available_kw=[1.0],
        solver_available_kw=[1.0 + 1e-8],
        lp_curtailment_w=[-1e-5],
    )
    assert result is not None
    assert result.planned_forecast_values_kw == [1.00000001]
    assert result.curtailment_values_kw == [0.0]

    assert derive_solar_forecast_provenance(
        raw_weather_kw=[1.0],
        adjusted_available_kw=[1.0],
        solver_available_kw=[1.1],
        lp_curtailment_w=[0.0],
    ) is None


def test_api_contract_is_schedule_aligned_and_additive():
    api_source = _COORDINATOR_SOURCE[
        _COORDINATOR_SOURCE.index("def get_api_data"):
        _COORDINATOR_SOURCE.index("async def set_settings")
    ]
    assert 'data["forecast_series"]' in api_source
    assert '"timestamps": list(api_response["timestamps"])' in api_source
    assert '"raw_forecast_values_kw": raw' in api_source
    assert '"planned_forecast_values_kw": planned' in api_source
    assert '"curtailment_values_kw": curtailed' in api_source
    assert '"load_forecast_values_kw": load' in api_source
    assert "len(raw) == len(planned) == len(curtailed) == len(load)" in api_source
    assert "== n_sched" in api_source
    assert 'api_response["plan_snapshot_id"] = plan_snapshot_id' in api_source
    assert 'data["forecast_evidence"]' in api_source
    # Stamped once every EV series is in the canonical snapshot (e3e10740).
    assert '"plan_snapshot_id": None' in api_source
    assert 'data["forecast_evidence"]["plan_snapshot_id"] = plan_snapshot_id' in api_source


def _publish_forecast_series(solar, load, provenance=None, available=True):
    """Execute the real API publication block with one completed plan snapshot."""
    tree = ast.parse(_COORDINATOR_SOURCE)
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_api_data")
    schedule_block = next(n for n in method.body if isinstance(n, ast.If)
                          and ast.unparse(n.test) == "self._current_schedule")
    start = next(i for i, n in enumerate(schedule_block.body)
                 if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "snapshot_payload") + 1
    end = next(i for i, n in enumerate(schedule_block.body)
               if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "display_import")
    raw, planned, curtailed = provenance or (None, None, None)
    coordinator = SimpleNamespace(
        _config=SimpleNamespace(interval_minutes=5),
        _last_solar_forecast=solar, _last_load_forecast=load,
        _has_solar_forecast=available,
        _last_raw_solar_forecast=raw, _last_planned_solar_forecast=planned,
        _last_solar_curtailment_forecast=curtailed,
    )
    namespace = dict(self=coordinator, data={}, n_sched=2,
                     api_response={"timestamps": ["2026-11-01T06:40:00+00:00", "2026-11-01T06:45:00+00:00"]})
    exec(compile(ast.Module(body=schedule_block.body[start:end], type_ignores=[]),
                 "forecast_api", "exec"), namespace)
    return namespace["data"]


def test_greedy_plan_keeps_available_solar_chart_without_inventing_lp_evidence():
    assert derive_solar_forecast_provenance([4, 2], [4, 2], [4, 2], None) is None
    data = _publish_forecast_series([4.0, 2.0], [1.0, 1.5])
    assert data["forecast_series"]["solar_forecast_values_kw"] == [4.0, 2.0]
    assert data["forecast_series"]["load_forecast_values_kw"] == [1.0, 1.5]
    assert len(data["forecast_series"]["timestamps"]) == 2
    assert "planned_forecast_values_kw" not in data["forecast_series"]
    assert "curtailment_values_kw" not in data["forecast_series"]
    assert "forecast_evidence" not in data


@pytest.mark.parametrize("solar,load,available", [
    (None, [1, 1], True), ([4], [1, 1], True), ([4, 2], [1], True),
    ([4, 2], [1, 1], False),
])
def test_input_forecast_chart_requires_available_aligned_inputs(solar, load, available):
    assert "forecast_series" not in _publish_forecast_series(solar, load, available=available)


def test_proven_lp_series_still_takes_precedence_over_input_forecast():
    data = _publish_forecast_series([4, 2], [1, 1], ([4, 2], [3, 1], [1, 1]))
    assert data["forecast_series"]["planned_forecast_values_kw"] == [3, 1]
    assert "solar_forecast_values_kw" not in data["forecast_series"]
    assert data["forecast_evidence"]["solar_forecast_values_kw"] == [3, 1]


def test_available_zero_solar_forecast_is_not_treated_as_missing():
    data = _publish_forecast_series([0.0, 0.0], [1.0, 1.0])
    assert data["forecast_series"]["solar_forecast_values_kw"] == [0.0, 0.0]
    assert "forecast_evidence" not in data
