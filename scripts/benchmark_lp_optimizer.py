#!/usr/bin/env python3
"""Reproducible, opt-in whole-cycle optimizer benchmarks (no hardware access).

Examples:
  python scripts/benchmark_lp_optimizer.py --case sunny-cost-neutral --runs 3
  python scripts/benchmark_lp_optimizer.py --case all --incumbents --jsonl /tmp/lp.jsonl
  python scripts/benchmark_lp_optimizer.py --component-root /path/to/baseline/custom_components/power_sync

Run one heavy benchmark per host. The default corpus is small; --case all also
exercises real 48-hour Cost Neutral, EV, final-solar and free-window models.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time
import types

ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.now = lambda *args, **kwargs: datetime(2026, 11, 7, tzinfo=timezone.utc)
    dt.utcnow = dt.now
    dt.UTC = timezone.utc
    util.dt = dt
    ha.util = util
    sys.modules.update({"homeassistant": ha, "homeassistant.util": util,
                        "homeassistant.util.dt": dt})
    for name, path in (("power_sync", COMPONENT_ROOT),
                       ("power_sync.optimization", COMPONENT_ROOT / "optimization")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package


@dataclass
class Case:
    inputs: dict
    config: dict | None = None
    attributes: dict | None = None


def _cases(module):
    n = 576
    timestamps = [datetime(2026, 11, 7, tzinfo=timezone.utc)
                  + timedelta(minutes=5*i) for i in range(n)]
    base = dict(import_prices=[0.25]*n, export_prices=[0.08]*n,
                solar_forecast=[0.0]*n, load_forecast=[0.7]*n,
                current_soc=0.5, allow_battery_export=False,
                schedule_timestamps=timestamps)
    ev = module.EVChargePlan(
        vehicle_id="benchmark", max_power_kw=(7.0,)*72+(0.0,)*(n-72),
        energy_needed_kwh=14.0, min_power_kw=1.4,
    )
    cases = {
        "flat-tariff": Case(base),
        "volatile-tariff": Case({**base,
            "import_prices": [0.08 if i % 12 < 4 else 0.35 for i in range(n)],
            "export_prices": [0.05 if i % 24 < 12 else 0.45 for i in range(n)],
            "allow_battery_export": [i % 24 >= 12 for i in range(n)]}),
        "positive-fit-cheap-import": Case({**base,
            "import_prices": [0.069]*202+[0.2856]*(n-202),
            "export_prices": [0.12]*n, "current_soc":0.19,
            "acquisition_cost_kwh":0.0, "allow_battery_export":True}),
        "solar-no-grid-charge": Case({**base,
            "solar_forecast":[5.0 if 96 <= i < 192 else 0.0 for i in range(n)],
            "current_soc":0.2, "allow_grid_charge":False}),
        "ev-48h": Case({**base, "ev_plan":ev}),
    }
    # Fixed deterministic full-horizon profiles; never pad a short saved capture.
    hours = [(i % 288)/12 for i in range(n)]
    imports = [0.0 if 12 <= h < 14 else 0.1 if h < 6 else 0.35 for h in hours]
    exports = [0.4 if 17 <= h < 19 else 0.08 for h in hours]
    solar = [round(max(0.0, 6*math.sin(math.pi*(h-6)/12)), 6) for h in hours]
    load = [1.1 if 17 <= h < 22 else 0.7 for h in hours]
    days = {"2026-11-07":1.8, "2026-11-08":1.8}
    def plan(*, ledger=False):
        return module.CostNeutralPlan(
            day_ids=[t.date().isoformat() for t in timestamps],
            earnings_caps_by_day=days, forecast_import_costs_by_day={},
            fixed_cost_allowances_by_day=days, current_day="2026-11-07",
            settlement_import_prices=imports, settlement_export_prices=exports,
            forecast_natural_export_earnings_by_day=(
                {day:1.2 for day in days} if ledger else {}),
        )
    cn = {**base, "import_prices":imports, "export_prices":exports,
          "solar_forecast":solar, "load_forecast":load,
          "allow_battery_export":True, "cost_neutral_plan":plan()}
    cases["cost-neutral-48h"] = Case(cn)
    cases["cost-neutral-ev-48h"] = Case({**cn, "ev_plan":ev})
    cases["cost-neutral-ledger-48h"] = Case({**cn, "cost_neutral_plan":plan(ledger=True)})
    fixture = json.loads((ROOT/"tests/fixtures/ticket_410_sunny_fallback.json").read_text())
    rows = [row[1:] for row in fixture["runs"] for _ in range(row[0])]
    imp, exp, pv, house = map(list, zip(*rows))
    start = datetime.fromisoformat(fixture["start"])
    ts = [start+timedelta(minutes=5*i) for i in range(len(rows))]
    caps = {"2026-11-07":1.841568, "2026-11-08":0.0, "2026-11-09":1.87}
    cases["sunny-cost-neutral"] = Case(dict(
        import_prices=imp, export_prices=exp, solar_forecast=pv, load_forecast=house,
        current_soc=0.052, allow_battery_export=True, disable_idle=True,
        schedule_timestamps=ts, cost_neutral_plan=module.CostNeutralPlan(
            day_ids=[t.date().isoformat() for t in ts], earnings_caps_by_day=caps,
            forecast_import_costs_by_day={}, fixed_cost_allowances_by_day={},
            current_day="2026-11-07", settlement_import_prices=imp,
            settlement_export_prices=exp)),
        config=dict(capacity_wh=24180, max_charge_w=12600, max_discharge_w=14400,
                    max_grid_export_w=5000, backup_reserve=0.05, hardware_reserve=0.05),
        attributes=dict(pre_window_slot=next(i for i,t in enumerate(ts)
                                            if t.hour==16 and t.minute==0),
                        pre_window_soc_target=1.0))
    free = json.loads((ROOT/"tests/fixtures/cost_neutral_free_window_paid_topups.json").read_text())
    start = datetime.fromisoformat(free["start"])
    ts = [start+timedelta(minutes=free["interval_minutes"]*i)
          for i in range(len(free["import_prices"]))]
    caps = free["cost_neutral_fixed_allowances"]
    cases["free-charge-tiebreak"] = Case(dict(
        import_prices=free["import_prices"], export_prices=free["export_prices"],
        solar_forecast=free["solar_forecast"], load_forecast=free["load_forecast"],
        current_soc=free["initial_soc"], schedule_timestamps=ts,
        allow_battery_export=[p>0 for p in free["export_prices"]],
        cost_neutral_plan=module.CostNeutralPlan(
            day_ids=[t.date().isoformat() for t in ts], earnings_caps_by_day=caps,
            forecast_import_costs_by_day={}, fixed_cost_allowances_by_day=caps,
            current_day=start.date().isoformat(),
            settlement_import_prices=free["import_prices"],
            settlement_export_prices=free["export_prices"])),
        config=dict(capacity_wh=54000, max_charge_w=14700, max_discharge_w=21000,
                    max_grid_import_w=15000, max_grid_export_w=10000,
                    backup_reserve=0.38, hardware_reserve=0.0))
    return cases


def _number(value):
    return value if isinstance(value,(float,int)) and math.isfinite(value) else None


@contextmanager
def _observe_native(module, seed, incumbents):
    """Identical external observation works for baseline and candidate code."""
    constructor = module.highspy.Highs
    calls = []
    class ObservedHighs:
        def __init__(self):
            self.native = constructor()
            self.started = time.monotonic()
            self.stats = {}
            calls.append(self.stats)
            self.native.setOptionValue("random_seed", seed)
            if incumbents:
                events = self.stats["incumbents"] = []
                callback = getattr(self.native,"cbMipImprovingSolution",None)
                if callback is not None:
                    def observe(event):
                        d=event.data_out
                        events.append(dict(time_s=_number(d.running_time),
                            objective=_number(d.mip_primal_bound),
                            bound=_number(d.mip_dual_bound),gap=_number(d.mip_gap)))
                    callback.subscribe(observe)
                else:
                    self.stats["incumbents_unavailable"] = True
        def __getattr__(self,name):
            return getattr(self.native,name)
        def run(self):
            self.stats["load_time_s"] = time.monotonic()-self.started
            lp=self.native.getLp()
            kinds=list(lp.integrality_)
            indices=[i for i,k in enumerate(kinds) if k==module.highspy.HighsVarType.kInteger]
            self.stats.update(columns=lp.num_col_,rows=lp.num_row_,
                integer_columns=len(indices),
                fixed_integer_columns=sum(lp.col_lower_[i]==lp.col_upper_[i] for i in indices))
            started=time.monotonic()
            try:
                return self.native.run()
            finally:
                self.stats["native_time_s"]=time.monotonic()-started
                info=self.native.getInfo()
                self.stats.update(status=self.native.modelStatusToString(self.native.getModelStatus()),
                    objective=_number(self.native.getObjectiveValue()),
                    bound=_number(info.mip_dual_bound),gap=_number(info.mip_gap),
                    nodes=_number(info.mip_node_count))
    module.highspy.Highs=ObservedHighs
    try:
        yield calls
    finally:
        module.highspy.Highs=constructor


def _run_case(module, case, *, seed=0, incumbents=False):
    optimizer=module.BatteryOptimizer(**{
        "capacity_wh":13500,"max_charge_w":7000,"max_discharge_w":7000,
        "backup_reserve":0.2,"interval_minutes":5,"horizon_hours":48,
        **(case.config or {})})
    for name,value in (case.attributes or {}).items():
        setattr(optimizer,name,value)
    with _observe_native(module,seed,incumbents) as native:
        started=time.monotonic()
        result=optimizer.optimize(**case.inputs)
        elapsed=time.monotonic()-started
    if not native:
        raise RuntimeError("Benchmark did not exercise the native HiGHS backend")
    return dict(total_time_s=elapsed,input_slots=len(case.inputs["import_prices"]),
        input_hours=len(case.inputs["import_prices"])/12,seed=seed,
        configured_horizon_hours=48,solver_used=result.solver_used,
        feasible=result.feasible,predicted_cost=result.schedule.predicted_cost,
        native=native,lp_stats=result.lp_stats,
        schedule=[a.to_dict() for a in result.schedule.actions])


def main(argv=None):
    global COMPONENT_ROOT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--runs",type=int,default=1)
    parser.add_argument("--seeds",default="0",help="Comma-separated recorded seeds")
    parser.add_argument("--incumbents",action="store_true")
    parser.add_argument("--jsonl",type=Path)
    parser.add_argument("--component-root",type=Path,default=COMPONENT_ROOT)
    parser.add_argument("--list-cases",action="store_true")
    args=parser.parse_args(argv)
    if args.runs<1:
        parser.error("--runs must be positive")
    COMPONENT_ROOT=args.component_root.resolve()
    _install_stubs()
    module=importlib.import_module("power_sync.optimization.battery_optimizer")
    if not module.HIGHS_AVAILABLE:
        parser.error("highspy is required; greedy-only timings are not LP benchmarks")
    cases=_cases(module)
    if args.list_cases:
        print("\n".join(cases));return 0
    names=args.cases or ["flat-tariff","ev-48h","sunny-cost-neutral"]
    if "all" in names:
        names=list(cases)
    if set(names)-cases.keys():
        parser.error("Unknown case(s): "+", ".join(set(names)-cases.keys()))
    try:
        seeds=[int(value) for value in args.seeds.split(",")]
    except ValueError:
        parser.error("--seeds must be comma-separated integers")
    metadata=dict(python=platform.python_version(),platform=platform.platform(),
        highs=module.highspy.Highs().version(),component_root=str(COMPONENT_ROOT),
        source_sha256=hashlib.sha256((COMPONENT_ROOT/"optimization/battery_optimizer.py").read_bytes()).hexdigest(),
        callbacks=args.incumbents)
    output=args.jsonl.open("w") if args.jsonl else None
    failures=0
    try:
        for name in names:
            elapsed=[]
            for repeat in range(args.runs):
                for seed in seeds:
                    record=dict(case=name,repeat=repeat,environment=metadata)
                    try:
                        record.update(_run_case(module,cases[name],seed=seed,incumbents=args.incumbents))
                        elapsed.append(record["total_time_s"])
                    except Exception as error:
                        record.update(error=type(error).__name__,message=str(error),seed=seed)
                        failures+=1
                    line=json.dumps(record,allow_nan=False,default=str)
                    if output:
                        output.write(line+"\n");output.flush()
                    else:
                        print(line,flush=True)
            if elapsed:
                print(f"{name}: median whole-cycle {statistics.median(elapsed):.4f}s "
                      f"({len(elapsed)} samples; failures/timeouts remain in JSONL)",file=sys.stderr,flush=True)
    finally:
        if output:
            output.close()
    return int(failures>0)


if __name__=="__main__":
    raise SystemExit(main())
