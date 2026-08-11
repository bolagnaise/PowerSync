"""Regression tests for evidence-backed solar forecast provenance."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


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
