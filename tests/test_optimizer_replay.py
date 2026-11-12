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
    plans = [module.EVChargePlan(vehicle_id="VIN_CANARY_A", max_power_kw=(3.0,) * 8, energy_needed_kwh=1.0, deadline_requirements=((4, 1.0),)),
             module.EVChargePlan(vehicle_id="VIN_CANARY_B", max_power_kw=(3.0,) * 8, energy_needed_kwh=2.0, deadline_requirements=((3, 1.0), (7, 2.0)))]
    _, bundle, _ = capture_one(api, values={**inputs(), "ev_plan": plans})
    wire = json.dumps(bundle)
    assert "VIN_CANARY" not in wire
    restored = [contract.decode(value, module) for value in bundle["inputs"]["ev_plan"]]
    assert len({p.vehicle_id for p in restored}) == 2
    assert [p.energy_needed_kwh for p in restored] == [1.0, 2.0]
    assert [p.max_power_kw for p in restored] == [(3.0,) * 8] * 2
    assert [p.deadline_requirements for p in restored] == [((4, 1.0),), ((3, 1.0), (7, 2.0))]


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
    assert source_coverage(paths, COMPONENT, COMPONENT) == {paths[0]: "not-covered", paths[1]: "solver_v1"}


def test_environment_change_is_not_silently_attributed_to_candidate(api):
    _, bundle, _ = capture_one(api)
    from regression_runner.runtime import replay
    bundle["source"]["solver_version"] = "0.0.0"
    with pytest.raises(trusted.ContractError, match="dependency_image_mismatch"):
        replay(resign(bundle), COMPONENT, allow_changed_source=True)


@pytest.mark.parametrize("missing", [False, True])
def test_source_binding_refuses_an_uninstalled_or_drifted_dependency(api, monkeypatch, missing):
    from regression_runner import runtime
    module, _, _ = api
    versions = runtime.installed_dependencies()
    if missing:
        versions.pop("numpy")
    else:
        versions["numpy"] = "0.0.0"
    monkeypatch.setattr(runtime, "installed_dependencies", lambda: versions)
    with pytest.raises(trusted.ContractError, match="dependency_image_unavailable" if missing else "dependency_image_mismatch"):
        source_binding(COMPONENT, module)


def test_dependency_contract_does_not_silently_skip_unpinned_or_duplicate_entries():
    from regression_runner.runtime import verify_dependencies
    for requirements in (b"# no actual pins\n", b"numpy>=2\n", b"numpy==2.5.0\nnumpy==2.5.0\n"):
        with pytest.raises(trusted.ContractError, match="unsupported_dependency_contract"):
            verify_dependencies(requirements)


def test_capture_records_the_observed_clock_zone_and_explicit_argument_names(api, monkeypatch):
    from zoneinfo import ZoneInfo
    from regression_runner.runtime import install_stubs
    module, _, _ = api
    local = datetime(2026, 10, 4, 1, 30, tzinfo=ZoneInfo("Australia/Sydney"))
    monkeypatch.setattr(module.dt_util, "now", lambda: local)
    _, bundle, _ = capture_one(api)
    assert bundle["decision_time"] == local.isoformat()
    assert bundle["decision_timezone"] == "Australia/Sydney"
    assert set(bundle["provided_inputs"]) == set(inputs())
    assert "disable_idle" not in bundle["provided_inputs"]
    assert trusted.decision_clock(bundle).tzinfo.key == "Australia/Sydney"
    install_stubs(COMPONENT, trusted.decision_clock(bundle))
    from homeassistant.util import dt
    assert dt.now().tzinfo.key == "Australia/Sydney"
    assert dt.now(timezone.utc) == local.astimezone(timezone.utc)


def test_full_installed_image_drift_and_dataclass_schema_drift_are_rejected(api, monkeypatch):
    from regression_runner import runtime
    _, bundle, _ = capture_one(api)
    versions = runtime.installed_dependencies()
    monkeypatch.setattr(runtime, "installed_dependencies", lambda: {**versions, "unexpected-package": "1.0"})
    with pytest.raises(trusted.ContractError, match="source_environment_mismatch"):
        runtime.replay(bundle, COMPONENT)
    monkeypatch.setattr(runtime, "installed_dependencies", lambda: versions)
    monkeypatch.setitem(trusted.CLASS_FIELDS, "EVChargePlan", (*trusted.CLASS_FIELDS["EVChargePlan"], "new_required_field"))
    with pytest.raises(trusted.ContractError, match="unsupported_dataclass"):
        runtime.replay(bundle, COMPONENT)


def test_candidate_constructor_defaults_and_derivations_are_not_claimed_covered(tmp_path):
    source = (COMPONENT / "optimization/battery_optimizer.py").read_text()
    roots = [tmp_path / "baseline", tmp_path / "candidate"]
    for root in roots:
        (root / "optimization").mkdir(parents=True)
        (root / "optimization/battery_optimizer.py").write_text(source)
    path = "custom_components/power_sync/optimization/battery_optimizer.py"
    candidate = roots[1] / "optimization/battery_optimizer.py"
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BatteryOptimizer")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "optimize")
    found = False
    for arguments, defaults in ((method.args.args[-len(method.args.defaults):], method.args.defaults),
                                (method.args.kwonlyargs, method.args.kw_defaults)):
        for index, argument in enumerate(arguments):
            if argument.arg == "disable_idle":
                defaults[index] = ast.Constant(True)
                found = True
    assert found
    candidate.write_text(ast.unparse(tree))
    assert source_coverage([path], *roots)[path] == "not-covered"
    tree = ast.parse(source)
    config = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "update_config")
    config.body.append(ast.Assign(targets=[ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()), attr="charge_efficiency", ctx=ast.Store())], value=ast.Constant(0.123)))
    candidate.write_text(ast.unparse(ast.fix_missing_locations(tree)))
    assert source_coverage([path], *roots)[path] == "not-covered"
    tree = ast.parse(source)
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "optimize")
    method.body.append(ast.Pass())
    candidate.write_text(ast.unparse(tree))
    assert source_coverage([path], *roots)[path] == "solver_v1"
    for dependency in ("cost_neutral.py", "ev_load_plan.py"):
        full = "custom_components/power_sync/optimization/" + dependency
        assert source_coverage([full], *roots)[full] == "not-covered"


def test_compare_refuses_deterministic_replays_that_do_not_match_capture(api, tmp_path, monkeypatch):
    _, original, _ = capture_one(api)
    baseline = {"outcome": original["outcome"], "solver": original["result"]["solver"],
                "exception": original["result"]["exception"], "schedule": original["result"]["schedule"], "source": original["source"]}
    changed = copy.deepcopy(original)
    changed["result"]["schedule"][0]["power_w"] += 1
    path = tmp_path / "capture.json"
    path.write_bytes(trusted.canonical(resign(changed)))
    spec = importlib.util.spec_from_file_location("trusted_replay_cli_fidelity_test", ROOT / "scripts/replay_optimizer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    requests = []
    def worker(request):
        requests.append(request)
        return copy.deepcopy(baseline)
    monkeypatch.setattr(cli, "worker", worker)
    output = tmp_path / "report.json"
    assert cli.main(["compare", str(path), "--candidate-root", str(COMPONENT),
        "--expected-candidate-sha256", original["source"]["component_sha256"], "--output", str(output)]) == 1
    report = json.loads(output.read_bytes())
    assert report["status"] == "invalid"
    assert report["a_a"]["outcome"] == report["candidate_a_a"]["outcome"] == "exact"
    assert report["capture_fidelity"]["outcome"] == "changed"
    assert len(requests) == 4


def test_comparison_rejects_source_edits_after_workers(api, tmp_path, monkeypatch, capsys):
    from regression_runner.runtime import component_digest
    _, bundle, _ = capture_one(api)
    baseline = {"outcome": bundle["outcome"], "solver": bundle["result"]["solver"],
                "exception": None, "schedule": bundle["result"]["schedule"], "source": bundle["source"]}
    root = tmp_path / "candidate"
    root.mkdir()
    path = root / "reviewed.py"
    path.write_text("# approved\n")
    expected = component_digest(root)
    capture = tmp_path / "capture.json"
    capture.write_bytes(trusted.canonical(bundle))
    spec = importlib.util.spec_from_file_location("trusted_replay_source_race_test", ROOT / "scripts/replay_optimizer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    requests = []
    def worker(request):
        requests.append(request)
        if len(requests) == 4:
            path.write_text("# changed after execution\n")
        return copy.deepcopy(baseline)
    monkeypatch.setattr(cli, "worker", worker)
    assert cli.main(["compare", str(capture), "--candidate-root", str(root), "--expected-candidate-sha256", expected]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "invalid", "error": "source_changed_during_comparison"}


def test_unscored_command_changes_cannot_pass_as_economic_equivalence(api):
    _, bundle, _ = capture_one(api)
    baseline = {"outcome": bundle["outcome"], "solver": bundle["result"]["solver"], "schedule": bundle["result"]["schedule"]}
    for key, value in (("action", "self_consumption"), ("power_w", 123456), ("grid_import_target_w", 123456), ("soc", None)):
        target = copy.deepcopy(baseline)
        if key == "action" and target["schedule"][0][key] == value:
            value = "charge"
        target["schedule"][0][key] = value
        assert classify(bundle, baseline, target)["outcome"] in ("unsupported", "violation")


def test_callbacks_static_and_external_helper_references_are_frozen(tmp_path):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    path = "custom_components/power_sync/optimization/battery_optimizer.py"
    template = '''class BatteryOptimizer:
    def __init__(self):
        CALLBACK
    def optimize(self):
        return self.helper()
    def helper(self):
        return VALUE
'''
    for root in (baseline, candidate):
        (root / "optimization").mkdir(parents=True)
    for callback, external in (("self.callback = self.helper", ""), ("BatteryOptimizer.helper(self)", ""),
                               ('getattr(self, "helper")()', ""), ("pass", "result = optimizer.helper()\n"),
                               ('getattr(self, "hel" + mode)()', ""), ("pass", 'result = getattr(optimizer, name)()\n')):
        for root, value in ((baseline, "1"), (candidate, "2")):
            (root / "optimization/battery_optimizer.py").write_text(template.replace("CALLBACK", callback).replace("VALUE", value))
            (root / "coordinator.py").write_text(external)
        assert source_coverage([path], baseline, candidate)[path] == "not-covered"
    for alias in ("public = helper", "value = property(helper)"):
        for root, value in ((baseline, "1"), (candidate, "2")):
            (root / "optimization/battery_optimizer.py").write_text(template.replace("CALLBACK", "pass").replace("VALUE", value) + "    " + alias + "\n")
            (root / "coordinator.py").write_text("result = optimizer.public()\n")
        assert source_coverage([path], baseline, candidate)[path] == "not-covered"


def test_source_digest_refuses_symlinks_and_sourceless_bytecode(tmp_path):
    from regression_runner.runtime import component_digest
    external = tmp_path / "external.py"
    external.write_text("# outside source tree\n")
    root = tmp_path / "source"
    root.mkdir()
    link = root / "module.py"
    link.symlink_to(external)
    with pytest.raises(trusted.ContractError, match="unsupported_source_symlink"):
        component_digest(root)
    link.unlink()
    (root / "module.pyc").write_bytes(b"unreviewed code")
    with pytest.raises(trusted.ContractError, match="unsupported_sourceless_bytecode"):
        component_digest(root)
    (root / "module.pyc").unlink()
    from importlib.machinery import EXTENSION_SUFFIXES
    (root / ("module" + EXTENSION_SUFFIXES[0])).write_bytes(b"unreviewed native code")
    with pytest.raises(trusted.ContractError, match="unsupported_native_component"):
        component_digest(root)


@pytest.mark.parametrize("comparison,control,reconstruction,uncovered,candidate,expected", [
    ("exact", "exact", "exact", False, "exact", "pass"),
    ("violation", "exact", "exact", True, "exact", "fail"),
    ("changed", "exact", "exact", True, "exact", "fail"),
    ("unsupported", "exact", "exact", False, "exact", "insufficient"),
    ("exact", "exact", "exact", True, "exact", "insufficient"),
    ("changed", "changed", "exact", False, "exact", "invalid"),
    ("exact", "exact", "changed", False, "exact", "invalid"),
    ("exact", "exact", "exact", False, "changed", "invalid"),
])
def test_run_status_preserves_failures_and_requires_faithful_repeatable_runs(comparison, control, reconstruction, uncovered, candidate, expected):
    from regression_runner.compare import run_status
    assert run_status({"outcome": comparison}, {"outcome": control}, {"outcome": reconstruction},
        {"path": "not-covered"} if uncovered else {}, {"outcome": candidate}) == expected


def test_unscored_active_policy_cannot_receive_equivalence_or_partial_cashflows(api):
    _, bundle, _ = capture_one(api)
    baseline = {"outcome": "feasible", "solver": bundle["result"]["solver"], "schedule": bundle["result"]["schedule"]}
    candidate = copy.deepcopy(baseline)
    candidate["schedule"][0]["power_w"] += 1
    variants = [
        ({"export_reserve_floor": 0.9, "allow_battery_export": True}, {}, "export_reserve_policy"),
        ({}, {"hardware_reserve_known": True, "hardware_reserve": 0.8}, "hardware_reserve_policy"),
        ({"allow_battery_export": True}, {"max_battery_export_w": 10}, "battery_export_limit_policy"),
        ({}, {"grid_charge_soc_cap": 0.6}, "grid_charge_soc_cap_policy"),
        ({"priority_export_enabled": True}, {}, "priority_export_policy"),
        ({"disable_idle": True}, {}, "disable_idle_policy"),
    ]
    for changed_inputs, changed_state, metric in variants:
        case = copy.deepcopy(bundle)
        case["inputs"].update(changed_inputs)
        case["state"].update(changed_state)
        result = classify(case, baseline, candidate)
        assert result["outcome"] in ("unsupported", "violation")
        assert metric in result["candidate_score"]["unsupported_metrics"]
    curtailed = copy.deepcopy(bundle)
    curtailed["inputs"]["solar_forecast"] = [100.0] * 8
    curtailed["inputs"]["grid_export_limits_w"] = [1.0] * 8
    value = score(curtailed, baseline["schedule"])
    assert not value["accounting_complete"]
    assert value["import_cost"] is value["export_revenue"] is value["tariff_net_cost"] is None
    malformed = copy.deepcopy(candidate)
    malformed["schedule"].pop()
    assert classify(bundle, baseline, malformed)["outcome"] == "violation"


def test_contract_rejects_boolean_versions_incoherent_outcomes_and_huge_numbers(api):
    _, bundle, _ = capture_one(api)
    for field in ("version", "outcome", "integer"):
        value = copy.deepcopy(bundle)
        if field == "version":
            value["schema_version"] = True
        elif field == "outcome":
            value["result"]["feasible"] = False
        else:
            value["inputs"]["import_prices"][0] = 10 ** 400
        with pytest.raises(trusted.ContractError):
            trusted.validate_bundle(resign(value))


def test_partial_cycle_is_discarded_and_snapshot_finish_cannot_contaminate_next_cycle(api, monkeypatch):
    from threading import Event, Thread
    module, capture, _ = api
    _, _, first = capture_one(api)
    retained = first.export()["estimated_bytes"]
    recorder = capture.CaptureRecorder(first.source, currency="AUD", max_bytes=retained + 128)
    optimizer = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    with capture.record_cycle(optimizer, recorder):
        optimizer.optimize(**inputs())
        assert len(recorder.active["attempts"]) == 1
        optimizer.optimize(**inputs())
        assert not recorder.active["complete"]
    assert recorder.export()["cycles"] == []
    assert recorder.dropped == 1

    recorder = capture.CaptureRecorder(first.source, currency="AUD")
    recorder.start_cycle()
    optimizer._regression_recorder = recorder
    entered, release, restarted = Event(), Event(), Event()
    original = capture.state_snapshot
    count = [0]
    errors = []
    def snapshot(*args):
        count[0] += 1
        if count[0] == 2:
            entered.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(capture, "state_snapshot", snapshot)
    def solve():
        try:
            optimizer.optimize(**inputs())
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=solve)
    lifecycle = Thread(target=lambda: (recorder.start_cycle(), restarted.set()))
    worker.start()
    try:
        assert entered.wait(5)
        lifecycle.start()
        assert restarted.wait(2), "lifecycle waited for an orphaned snapshot"
        assert recorder.active is None
    finally:
        release.set()
        worker.join(5)
        if lifecycle.ident is not None:
            lifecycle.join(5)
    assert not errors
    assert not worker.is_alive() and not lifecycle.is_alive()
    assert recorder.generation == 2 and recorder.active is None
    assert recorder.errors == 1 and recorder.dropped == 2


def test_worker_hash_seed_and_pre_dispatch_source_approval(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("trusted_replay_cli_seed_test", ROOT / "scripts/replay_optimizer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    request = {"operation": "probe", "component_root": str(tmp_path)}
    assert cli.worker(request)["hash_seed_probe"] == cli.worker(request)["hash_seed_probe"]
    def forbidden(*args, **kwargs):
        raise AssertionError("unapproved source received a dispatch")
    monkeypatch.setattr(cli.subprocess, "run", forbidden)
    with pytest.raises(trusted.ContractError, match="source_changed_before_dispatch"):
        cli.worker({**request, "expected_component_sha256": "0" * 64})


def test_replay_refuses_unmodeled_persistent_state_changes(api, monkeypatch):
    from functools import wraps
    from regression_runner import runtime
    module, _, _ = api
    _, bundle, _ = capture_one(api)
    original = module.BatteryOptimizer.optimize
    @wraps(original)
    def mutate(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        self.terminal_weight = 123.456
        return result
    monkeypatch.setattr(module.BatteryOptimizer, "optimize", mutate)
    monkeypatch.setattr(runtime, "load_component", lambda *args: module)
    with pytest.raises(trusted.ContractError, match="unsupported_state_mutation"):
        runtime.replay(bundle, COMPONENT)


def test_actual_coordinator_stage_guard_preserves_observer_and_aborts_bad_capture(api):
    module, capture, _ = api
    _, _, first = capture_one(api)
    recorder = capture.CaptureRecorder(first.source, currency="AUD")
    recorder.start_cycle()
    optimizer = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    optimizer._regression_recorder = recorder
    optimizer.optimize(**inputs())
    schedule = SimpleNamespace(actions=[SimpleNamespace(timestamp=NOW, action="idle", power_w=float("nan"),
        soc=0.5, battery_charge_w=0.0, battery_discharge_w=0.0, ev_charge_w=0.0, grid_import_target_w=None)])
    tree = ast.parse((COMPONENT / "optimization/coordinator.py").read_text())
    def named_if(node, name):
        return isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == name
    def calls(node, method, argument=None):
        return any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == method and
            (argument is None or any(isinstance(value, ast.Constant) and value.value == argument for value in item.args))
            for item in ast.walk(node))
    stage = next(node for node in ast.walk(tree) if named_if(node, "regression_recorder") and calls(node, "record_stage"))
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "support_observer" for target in node.targets))
    observer = next(node for node in ast.walk(tree) if named_if(node, "support_observer"))
    cleanup = next(node for node in ast.walk(tree) if named_if(node, "regression_recorder") and calls(node, "finish_cycle", "aborted"))
    observed = []
    owner = SimpleNamespace(_optimizer=optimizer, _support_plan_observer=lambda *args: observed.append(args), _config=SimpleNamespace())
    namespace = {"regression_recorder": recorder, "result": SimpleNamespace(schedule=schedule), "self": owner,
        **{name: [] for name in ("import_prices", "export_prices", "solar_forecast", "load_forecast")}}
    code = compile(ast.fix_missing_locations(ast.Module(body=[stage, assignment, observer, cleanup], type_ignores=[])), str(COMPONENT / "optimization/coordinator.py"), "exec")
    exec(code, namespace)
    assert len(observed) == 1 and observed[0][0] is schedule
    assert recorder.errors == 1 and recorder.export()["cycles"] == []
    assert optimizer._regression_recorder is None


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
    import py_compile
    spec = importlib.util.spec_from_file_location("trusted_replay_cli_test", ROOT / "scripts/replay_optimizer.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    optimization = tmp_path / "optimization"
    optimization.mkdir()
    source = optimization / "battery_optimizer.py"
    poison = tmp_path / "poison.py"
    poison.write_text('raise RuntimeError("stale unreviewed bytecode executed")\n')
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    py_compile.compile(str(poison), cfile=str(cache), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    poison.unlink()
    source.write_text('import socket\nsocket.create_connection(("127.0.0.1", 9))\n')
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


def test_closing_observation_erases_pending_copies_without_waiting_for_solver_lock(api):
    from threading import Event, Thread
    module, capture, _ = api
    optimizer = module.BatteryOptimizer(interval_minutes=60, horizon_hours=8)
    recorder = capture.CaptureRecorder(source_binding(COMPONENT, module), currency='AUD')
    recorder.start_cycle()
    bound = inspect.signature(optimizer.optimize).bind(**inputs())
    bound.apply_defaults()
    ticket = recorder.begin(optimizer, dict(bound.arguments), provided_inputs=list(inputs()), decision_time=NOW)
    assert ticket and recorder.pending
    held, release = Event(), Event()
    def hold():
        with recorder._lock:
            held.set();release.wait(5)
    thread = Thread(target=hold)
    thread.start();assert held.wait(2)
    try:
        recorder.close()
        assert ticket == {} and not recorder.pending and not recorder.cycles and recorder.active is None
        assert recorder.closed
    finally:
        release.set();thread.join(2)
    assert not thread.is_alive()
