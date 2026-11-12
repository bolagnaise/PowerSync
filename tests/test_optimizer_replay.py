"""Real solver replay, independent seeded checks and capture non-interference."""
from __future__ import annotations

import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from regression_runner import trusted_contract as trusted
from regression_runner.compare import classify, source_coverage
from regression_runner.runtime import dependency_inventory, load_component, source_binding
from regression_runner.scoring import score

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
COMPONENT = ROOT / "custom_components/power_sync"


@pytest.fixture
def api():
    def related(name):
        return name == "power_sync" or name.startswith("power_sync.") or name == "homeassistant" or name.startswith("homeassistant.")
    saved = {name: value for name, value in sys.modules.items() if related(name)}
    try:
        module = load_component(COMPONENT, NOW)
        capture = importlib.import_module("power_sync.regression.capture")
        contract = importlib.import_module("power_sync.regression.contract")
        yield module, capture, contract
    finally:
        for name in tuple(sys.modules):
            if related(name):
                del sys.modules[name]
        sys.modules.update(saved)


def inputs(n=8):
    return dict(import_prices=[0.05] * 4 + [0.45] * (n - 4),
                export_prices=[0.02] * n, solar_forecast=[0.0] * n,
                load_forecast=[0.5] * n, current_soc=0.5,
                schedule_timestamps=[NOW + timedelta(hours=i) for i in range(n)])


def capture_one(api, *, values=None, optimizer=None):
    module, capture, contract = api
    optimizer = optimizer or module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    recorder = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    with capture.record_cycle(optimizer, recorder):
        result = optimizer.optimize(**(values or inputs()))
    report = recorder.export()
    assert report["errors"] == 0, report
    return result, report["cycles"][0]["attempts"][0], recorder


def resign(bundle):
    bundle["digest"] = trusted.digest({k: v for k, v in bundle.items() if k != "digest"})
    return bundle


def test_contract_inventory_catches_new_input_or_attribute(api):
    module, _, contract = api
    assert tuple(inspect.signature(module.BatteryOptimizer.optimize).parameters)[1:] == contract.INPUT_FIELDS
    assert dependency_inventory(COMPONENT) == set(contract.STATE_FIELDS)
    for name, fields in contract.CLASS_FIELDS.items():
        from dataclasses import fields as dataclass_fields
        assert tuple(f.name for f in dataclass_fields(getattr(module, name))) == fields


def test_real_capture_roundtrip_and_independent_score(api):
    result, bundle, recorder = capture_one(api)
    assert trusted.loads_bundle(trusted.canonical(bundle)) == bundle
    from regression_runner.runtime import replay
    repeated = replay(bundle, COMPONENT)
    expected = {"outcome": bundle["outcome"], "schedule": bundle["result"]["schedule"], "solver": bundle["result"]["solver"]}
    comparison = classify(bundle, expected, repeated)
    assert comparison["outcome"] in ("exact", "equivalent"), comparison
    assert not comparison["candidate_score"]["violations"]
    assert comparison["candidate_score"]["counterfactual_daily_savings"] is None
    assert recorder.capture_seconds > 0


def test_temporary_override_and_retry_are_separate_attempts(api):
    module, capture, _ = api
    opt = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    rec = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    with capture.record_cycle(opt, rec):
        opt.update_config(backup_reserve=0.35)
        opt.optimize(**inputs())
        opt.update_config(backup_reserve=0.20)
        opt.optimize(**{**inputs(), "allow_grid_charge": False})
        rec.record_stage("committed", opt.optimize(**inputs()).schedule)
        rec.finish_cycle("committed")
    cycle = rec.export()["cycles"][0]
    assert [x["state"]["backup_reserve"] for x in cycle["attempts"]] == [0.35, 0.2, 0.2]
    assert cycle["attempts"][0]["inputs"]["allow_grid_charge"] is True
    assert cycle["attempts"][1]["inputs"]["allow_grid_charge"] is False
    assert cycle["outcome"] == "committed"
    assert cycle["stages"][0]["stage"] == "committed"


def test_recorder_failure_does_not_change_solve(api, monkeypatch):
    module, capture, contract = api
    opt = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    first = opt.optimize(**inputs())
    recorder = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    def fail(*args, **kwargs):
        raise RuntimeError("CANARY_SECRET")
    monkeypatch.setattr(recorder, "begin", fail)
    with capture.record_cycle(opt, recorder):
        second = opt.optimize(**inputs())
    assert contract.schedule_snapshot(first.schedule, contract.Codec()) == contract.schedule_snapshot(second.schedule, contract.Codec())
    report = recorder.export()
    assert report["cycles"] == []
    assert report["errors"] == 1
    assert "CANARY_SECRET" not in json.dumps(report)


def test_exception_inputs_preserved_without_raw_message(api, monkeypatch):
    module, capture, _ = api
    opt = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    recorder = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    # Inject before solver fallback: the normal public call still raises.
    monkeypatch.setattr(opt, "_align_forecasts", lambda *args: (_ for _ in ()).throw(ValueError("CANARY_SECRET")))
    with pytest.raises(ValueError, match="CANARY_SECRET"):
        with capture.record_cycle(opt, recorder):
            opt.optimize(**inputs())
    report = recorder.export()
    bundle = report["cycles"][0]["attempts"][0]
    assert bundle["outcome"] == "exception"
    assert bundle["result"]["exception"] == "ValueError"
    assert "CANARY_SECRET" not in json.dumps(report)


def test_full_queue_discards_whole_cycle(api):
    module, capture, _ = api
    opt = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    rec = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD", max_bytes=1024)
    with capture.record_cycle(opt, rec):
        opt.optimize(**inputs())
        opt.optimize(**inputs())
    assert rec.export()["cycles"] == []
    assert rec.dropped == 1


def test_export_is_detached(api):
    _, bundle, rec = capture_one(api)
    first = rec.export()
    first["cycles"][0]["attempts"][0]["inputs"]["current_soc"] = -1
    assert rec.export()["cycles"][0]["attempts"][0]["inputs"]["current_soc"] == 0.5


def test_missing_field_and_nonfinite_values_fail_closed(api):
    _, bundle, _ = capture_one(api)
    del bundle["inputs"]["price_valid_slots"]
    with pytest.raises(trusted.ContractError):
        trusted.validate_bundle(resign(bundle))
    with pytest.raises(trusted.ContractError):
        trusted.loads_bundle(b'{"bad":NaN}')
    with pytest.raises(trusted.ContractError):
        trusted.loads_bundle(b'{"schema_version":1,"schema_version":1}')


def test_ev_aliases_preserve_identity_and_deadlines(api):
    module, _, contract = api
    plans = [module.EVChargePlan(vehicle_id="VIN_CANARY_A", max_power_kw=(3.0,) * 8, energy_needed_kwh=1.0),
             module.EVChargePlan(vehicle_id="VIN_CANARY_B", max_power_kw=(3.0,) * 8, energy_needed_kwh=2.0)]
    _, bundle, _ = capture_one(api, values={**inputs(), "ev_plan": plans})
    wire = json.dumps(bundle)
    assert "VIN_CANARY" not in wire
    restored = [contract.decode(value, module) for value in bundle["inputs"]["ev_plan"]]
    assert len({p.vehicle_id for p in restored}) == 2
    assert [p.energy_needed_kwh for p in restored] == [1.0, 2.0]
    assert [p.max_power_kw for p in restored] == [(3.0,) * 8] * 2


def test_unknown_free_text_is_not_disclosed(api):
    module, capture, _ = api
    opt = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    rec = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    with capture.record_cycle(opt, rec):
        opt.optimize(**{**inputs(), "manual_control": {"credential": "CANARY_SECRET"}})
    assert rec.export()["cycles"] == []
    assert rec.errors == 1


def test_external_mutation_makes_attempt_torn(api, monkeypatch):
    module, capture, _ = api
    opt = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    actual = opt._align_forecasts
    def mutate(*args):
        opt.terminal_weight = 9.0
        return actual(*args)
    monkeypatch.setattr(opt, "_align_forecasts", mutate)
    _, bundle, _ = capture_one(api, optimizer=opt)
    assert bundle["coverage"]["torn"]
    with pytest.raises(trusted.ContractError, match="incomplete_capture"):
        trusted.validate_bundle(bundle)


def test_missing_clock_is_explicitly_incomplete(api):
    _, bundle, _ = capture_one(api, values={**inputs(), "schedule_timestamps": None})
    assert not bundle["coverage"]["complete"]


def test_seeded_reserve_and_slot_defects_are_detected(api):
    _, bundle, _ = capture_one(api)
    rows = copy.deepcopy(bundle["result"]["schedule"])
    rows[0]["battery_discharge_w"] = 999_000
    rules = {v["rule"] for v in score(bundle, rows)["violations"]}
    assert {"battery_power_limit", "soc_limit", "soc_energy_balance"} <= rules
    rows = copy.deepcopy(bundle["result"]["schedule"])
    rows[0]["timestamp"] = rows[1]["timestamp"]
    assert "timestamp_alignment" in {v["rule"] for v in score(bundle, rows)["violations"]}


def test_currency_and_terminal_energy_are_explicit(api):
    _, bundle, _ = capture_one(api)
    value = score(bundle, bundle["result"]["schedule"], terminal_price=0.4)
    assert value["currency"] == "AUD"
    assert value["common_score"] == pytest.approx(value["tariff_net_cost"] - value["terminal_energy_delta_kwh"] * 0.4)


def test_coordinator_edits_are_not_solver_coverage():
    paths = ["custom_components/power_sync/optimization/coordinator.py", "custom_components/power_sync/optimization/battery_optimizer.py"]
    assert source_coverage(paths) == {paths[0]: "not-covered", paths[1]: "solver_v1"}


def test_environment_change_is_not_silently_attributed_to_candidate(api):
    _, bundle, _ = capture_one(api)
    from regression_runner.runtime import replay
    bundle["source"]["solver_version"] = "0.0.0"
    with pytest.raises(trusted.ContractError, match="dependency_image_mismatch"):
        replay(resign(bundle), COMPONENT, allow_changed_source=True)


def test_nondefault_diagnostic_solver_options_are_not_complete_capture(api):
    module, capture, _ = api
    optimizer = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    rec = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    with capture.record_cycle(optimizer, rec), module.solver_benchmark_diagnostics(seed=42):
        assert optimizer.optimize(**inputs()).feasible
    assert rec.export()["cycles"] == []
    assert rec.errors == 1


def test_backend_flip_is_an_execution_path_change():
    result = classify({}, {"outcome": "feasible", "solver": "highs"}, {"outcome": "feasible", "solver": "greedy"})
    assert result == {"outcome": "changed", "reason": "solver_backend_changed"}


@pytest.mark.parametrize("price,solar,load,expected", [(0.1, 1.0, 0.0, -0.2), (-0.1, 1.0, 0.0, 0.2), (-0.1, 0.0, 1.0, -0.2)])
def test_independent_cashflow_signs_with_hand_calculated_values(api, price, solar, load, expected):
    module, _, contract = api
    values = {"import_prices": [price] * 2, "export_prices": [price] * 2,
        "solar_forecast": [solar] * 2, "load_forecast": [load] * 2, "current_soc": 0.5,
        "schedule_timestamps": [NOW, NOW + timedelta(hours=1)]}
    _, bundle, _ = capture_one(api, values=values, optimizer=module.BatteryOptimizer(interval_minutes=60, horizon_hours=2))
    rows = [{"timestamp": contract.Codec().encode(at), "action": "idle", "power_w": 0.0,
             "soc": 0.5, "battery_charge_w": 0.0, "battery_discharge_w": 0.0,
             "ev_charge_w": 0.0, "grid_import_target_w": None} for at in values["schedule_timestamps"]]
    scored = score(bundle, rows)
    assert not scored["violations"]
    assert scored["tariff_net_cost"] == pytest.approx(expected)


def test_worker_blocks_network_during_component_import(tmp_path):
    spec = importlib.util.spec_from_file_location("trusted_replay_cli_test", ROOT / "scripts/replay_optimizer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    optimization = tmp_path / "optimization"
    optimization.mkdir()
    (optimization / "battery_optimizer.py").write_text('import socket\nsocket.create_connection(("127.0.0.1", 9))\n')
    report = cli.worker({"operation": "capture", "component_root": str(tmp_path), "case": "flat-tariff", "currency": "AUD"})
    assert report == {"worker_error": "forbidden_io"}


def test_private_export_restricts_existing_file_and_refuses_symlink(api, tmp_path, monkeypatch):
    _, bundle, _ = capture_one(api)
    spec = importlib.util.spec_from_file_location("trusted_replay_cli_export_test", ROOT / "scripts/replay_optimizer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "worker", lambda request: bundle)
    output = tmp_path / "case.json"
    output.write_text("old")
    output.chmod(0o644)
    assert cli.main(["capture", "--currency", "AUD", "--output", str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    victim = tmp_path / "keep.json"
    victim.write_text("do not overwrite")
    link = tmp_path / "linked.json"
    link.symlink_to(victim)
    assert cli.main(["capture", "--currency", "AUD", "--output", str(link)]) == 2
    assert victim.read_text() == "do not overwrite"


def test_changed_files_are_discovered_without_manual_coverage_flags(tmp_path):
    from regression_runner.compare import changed_component_paths
    roots = [tmp_path / "released", tmp_path / "candidate"]
    for root in roots:
        (root / "optimization").mkdir(parents=True)
        (root / "optimization/coordinator.py").write_text("old")
    (roots[1] / "optimization/coordinator.py").write_text("new")
    paths = changed_component_paths(*roots)
    assert source_coverage(paths) == {"custom_components/power_sync/optimization/coordinator.py": "not-covered"}


@pytest.mark.asyncio
async def test_real_coordinator_closure_records_override_and_restores_on_error(api):
    module, capture, _ = api
    tree = ast.parse((COMPONENT / "optimization/coordinator.py").read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_optimizer_once")
    optimizer = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    async def executor(function, *arguments):
        return function(*arguments)
    owner = SimpleNamespace(_optimizer=optimizer, hass=SimpleNamespace(async_add_executor_job=executor),
        _config=SimpleNamespace(backup_reserve=0.2, allow_grid_charge=True),
        _cost_function=SimpleNamespace(value="cost"), _last_zerohero_bonus_prices=None,
        _last_zerohero_bonus_cap_kwh=None, _last_zerocharge_bonus_prices=None,
        _last_zerocharge_bonus_cap_kwh=None, _last_import_bonus_group_ids=None,
        _last_export_bonus_group_ids=None, _last_optimizer_price_valid_slots=None,
        _should_disable_idle_schedule=lambda: False, _battery_provenance_is_usable=lambda: False)
    values = inputs()
    namespace = {"OptimizerResult": module.OptimizerResult, "self": owner,
        "import_prices": values["import_prices"], "export_prices": values["export_prices"],
        "solar_forecast": values["solar_forecast"], "load_forecast": values["load_forecast"],
        "soc": 0.5, "acq_cost": 0.0, "battery_export_allowed": False,
        "battery_charge_blocked": False, "grid_charge_allowed": None,
        "schedule_timestamps": values["schedule_timestamps"], "priority_export_slots": [False] * 8,
        "grid_export_limits_w": None, "cost_neutral_cap": None, "cost_neutral_slots": None,
        "cost_neutral_forecast_import_cost": 0.0, "cost_neutral_fixed_cost_allowance": None,
        "cost_neutral_plan": None, "profit_max_solar_export_slots": None,
        "manual_control_payload": None, "ev_charge_plan": None}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "coordinator-closure", "exec"), namespace)
    rec = capture.CaptureRecorder(source_binding(COMPONENT, module), currency="AUD")
    with capture.record_cycle(optimizer, rec):
        await namespace["_run_optimizer_once"](reserve_floor=0.35)
        assert optimizer.backup_reserve == 0.2
        original = optimizer._align_forecasts
        optimizer._align_forecasts = lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic"))
        with pytest.raises(RuntimeError):
            await namespace["_run_optimizer_once"](reserve_floor=0.4)
        optimizer._align_forecasts = original
        assert optimizer.backup_reserve == 0.2
    attempts = rec.export()["cycles"][0]["attempts"]
    assert [item["state"]["backup_reserve"] for item in attempts] == [0.35, 0.4]
    assert attempts[1]["outcome"] == "exception"
