"""Actual restoration observation and independent measured-calibration profile."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from regression_runner.temporal import coordinator,replay
from regression_runner.evaluation import measured_calibration,evaluate_plant
from regression_runner.plant import PlantError


def capture_module():
    spec=importlib.util.spec_from_file_location('isolated_temporal_capture',ROOT/'custom_components/power_sync/regression/temporal_capture.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_actual_restore_observation_replays_negative_and_positive_ack_without_changing_controls():
    capture=capture_module();cls,_=coordinator(ROOT/'custom_components/power_sync')
    function=cls._restore_pre_idle_backup_reserve
    function.__globals__.update(_temporal_monitoring=capture.monitoring,_temporal_boundary=capture.boundary,_temporal_exception=capture.boundary_exception)
    cls._restore_pre_idle_backup_reserve=capture.observe_reserve_restore(function)
    owner=cls();owner._pre_idle_backup_reserve=20;owner._idle_hold_reserve=60;owner._last_executed_action='self_consumption'
    owner._scheduled_ev_no_discharge_active=False;owner._config=SimpleNamespace(backup_reserve=.2)
    owner._monitoring_mode_active=lambda:False
    recorder=capture.TemporalCapture();owner._regression_temporal_recorder=recorder
    calls=[];results=[False,True]
    class Battery:
        async def set_backup_reserve(self,value):
            calls.append(value);return results.pop(0)
    async def run():
        assert await owner._restore_pre_idle_backup_reserve(Battery()) is False
        assert await owner._restore_pre_idle_backup_reserve(Battery()) is True
        report=recorder.export()
        assert report['complete'] and len(report['sequences'])==2
        for row in report['sequences']:
            result=await replay(ROOT/'custom_components/power_sync',row['initial'],row['events'])
            assert {key:result[key] for key in ('commands','returns','state')}==row['expected']
    asyncio.run(run());assert calls==[20,20]
    assert owner._pre_idle_backup_reserve is None
    recorder.close();assert recorder.sequences==[]


def test_observer_failure_never_changes_the_actual_restore_result():
    capture=capture_module();cls,_=coordinator(ROOT/'custom_components/power_sync')
    function=cls._restore_pre_idle_backup_reserve
    cls._restore_pre_idle_backup_reserve=capture.observe_reserve_restore(function)
    owner=cls();owner._pre_idle_backup_reserve=20;owner._idle_hold_reserve=60
    owner._monitoring_mode_active=lambda:False
    class Broken:
        unsupported=0
        def begin(self,owner):
            raise RuntimeError('synthetic recorder failure')
    owner._regression_temporal_recorder=Broken()
    class Battery:
        async def set_backup_reserve(self,value):
            assert value==20;return True
    assert asyncio.run(owner._restore_pre_idle_backup_reserve(Battery())) is True
    assert owner._pre_idle_backup_reserve is None


def physical_case():
    powers=[4.5,4.5,4.5,-4.5,-4.5,-4.5,4.5,4.5,4.5,-4.5,-4.5,-4.5]
    rows=[];energy=.5
    for index,power in enumerate(powers):
        if index:
            average=(powers[index-1]+power)/2
            energy+=average/120*(.9 if average>=0 else 1/.9)
        rows.append(dict(at=100+index*30,soc=energy,power=power,load=.2,solar=0.,grid=.2+power,available=True))
    report=dict(profile='measured-battery-v1',rows=rows,unsupported=0,soc_resolution=.001,power_sign='positive_into_battery')
    bundle={'state':dict(capacity_kwh=1.,max_charge_kw=5.,max_discharge_kw=5.,charge_efficiency=.95,discharge_efficiency=.95,
                        hardware_reserve_known=True,hardware_reserve=.1,dt_hours=1/12),
            'inputs':dict(current_soc=.53,load_forecast=[.2],solar_forecast=[0.],import_prices=[.3],export_prices=[.1])}
    return report,bundle


def test_measured_profile_fits_independent_efficiency_and_requires_withheld_coverage():
    report,bundle=physical_case()
    parameters,validation=measured_calibration(report,bundle)
    assert parameters.charge_efficiency==pytest.approx(.9)
    assert parameters.discharge_efficiency==pytest.approx(.9)
    assert validation['valid']
    with pytest.raises(PlantError):
        measured_calibration({**report,'rows':report['rows'][:3]},bundle)
    with pytest.raises(PlantError):
        measured_calibration(report,{**bundle,'state':{**bundle['state'],'hardware_reserve_known':False}})


def test_common_forecasts_are_simulation_and_terminal_energy_drain_is_not_free_savings():
    report,bundle=physical_case()
    baseline={'schedule':[dict(battery_charge_w=0,battery_discharge_w=0)]}
    result=evaluate_plant(report,bundle,baseline,baseline,terminal_price=.3)
    assert result['status']=='pass' and result['semantics']=='counterfactual_under_common_forecasts'
    candidate={'schedule':[dict(battery_charge_w=0,battery_discharge_w=100)]}
    result=evaluate_plant(report,bundle,baseline,candidate,terminal_price=.3)
    assert result['status']=='fail' and result['common_score_delta']>0
