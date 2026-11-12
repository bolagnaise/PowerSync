"""Independent calibrated simulation. Never turn forecasts into observed savings."""
import math
from .plant import Parameters, PlantError, calibrate, paired, sensitivity
from .scoring import primitive


def measured_calibration(report,bundle):
    if type(report) is not dict or set(report)!={'profile','rows','unsupported','soc_resolution','power_sign'} or report['profile']!='measured-battery-v1' or report['power_sign']!='positive_into_battery' or report['unsupported'] or not 8<=len(report['rows'])<=32:
        raise PlantError('measured_coverage_missing')
    state=primitive(bundle['state'])
    if state['hardware_reserve_known'] is not True:
        raise PlantError('physical_reserve_unknown')
    parameters=Parameters(state['capacity_kwh'],state['max_charge_kw'],state['max_discharge_kw'],
        state['charge_efficiency'],state['discharge_efficiency'],state['hardware_reserve'],uncertainty_kwh=state['capacity_kwh']*.02)
    rows=report['rows'];intervals=[]
    for index,row in enumerate(rows):
        if type(row) is not dict or set(row)!={'at','soc','power','load','solar','grid','available'} or type(row['at']) is not int or type(row['available']) is not bool:
            raise PlantError('invalid_measured_row')
        if any(type(row[key]) not in (int,float) or not math.isfinite(row[key]) for key in ('soc','power','load','solar','grid')) or not 0<=row['soc']<=1 or min(row['load'],row['solar'])<0:
            raise PlantError('invalid_measured_row')
        if abs(row['grid']+row['solar']-row['load']-row['power'])>.25:
            raise PlantError('measured_meter_balance')
        if index:
            before=rows[index-1];seconds=row['at']-before['at']
            if not 1<=seconds<=60:
                raise PlantError('measured_time_gap')
            if row['available'] and before['available']:
                intervals.append({'at':row['at'],'before':before['soc']*parameters.capacity_kwh,
                    'after':row['soc']*parameters.capacity_kwh,'power':(row['power']+before['power'])/2,'hours':seconds/3600})
    resolution=report['soc_resolution']
    if type(resolution) not in (int,float) or not 0<resolution<=.01:
        raise PlantError('measured_resolution_unknown')
    split=len(intervals)*2//3
    fitted,validation=calibrate(parameters,intervals[:split],intervals[split:],
        tolerance_kwh=max(.05,2*resolution*parameters.capacity_kwh),resolution_kwh=resolution*parameters.capacity_kwh)
    if not validation['valid']:
        raise PlantError('withheld_validation_failed')
    return fitted,validation


def evaluate_plant(report,bundle,baseline,candidate,*,terminal_price):
    parameters,validation=measured_calibration(report,bundle)
    inputs=primitive(bundle['inputs']);state=primitive(bundle['state'])
    left,right=primitive(baseline['schedule']),primitive(candidate['schedule'])
    if len(left)!=len(right) or not left:
        raise PlantError('trajectory_coverage')
    exogenous=[]
    for index in range(len(left)):
        values={key:inputs[source][index] for key,source in (('load_kw','load_forecast'),('solar_kw','solar_forecast'),('import_price','import_prices'),('export_price','export_prices'))}
        values.update(hours=state['dt_hours'],available=True)
        exogenous.append(values)
    actions=lambda rows:[(row['battery_charge_w']-row['battery_discharge_w'])/1000 for row in rows]
    result=paired(parameters,inputs['current_soc'],exogenous,actions(left),actions(right),terminal_price=terminal_price)
    sensitivity_result=sensitivity(parameters,inputs['current_soc'],exogenous,actions(left),actions(right),terminal_price=terminal_price)
    # Every simulated state must stay within the independently withheld fit's
    # energy/power range. A short fit never licenses a whole-horizon extrapolation.
    from .plant import Plant
    for arm in (left,right):
        plant=Plant(parameters,inputs['current_soc'])
        for action,values in zip(actions(arm),exogenous):
            row=plant.step(action_kw=action,strict=False,**values)
            if not validation['min_energy_kwh']<=row['energy_kwh']<=validation['max_energy_kwh'] or abs(row['actual_power_kw'])>validation['max_power_kw']:
                raise PlantError('simulation_out_of_calibration_range')
    status='invalid' if sensitivity_result['baseline_violations'] else 'fail' if sensitivity_result['candidate_violations'] or sensitivity_result['max_common_score_delta']>1e-6 else 'pass'
    return {'status':status,'violations':sensitivity_result['candidate_violations'],'common_score_delta':sensitivity_result['max_common_score_delta'],
            'validation':'withheld_measured_window','sensitivity_arms':sensitivity_result['arms'],
            'semantics':'counterfactual_under_common_forecasts'}
