import sys
import hashlib
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from regression_runner.plant import Plant, Parameters, PlantError, calibrate, paired, sensitivity
from regression_runner.campaign import Campaign, CampaignError, acceptance, seven_day, holdout, HoldoutLedger


def test_independent_plant_conservation_losses_delay_and_unavailability():
    p = Parameters(10, 5, 5, charge_efficiency=.9, discharge_efficiency=.8, reserve=.1, delay_steps=1)
    plant = Plant(p, .5)
    base = dict(load_kw=1, solar_kw=0, hours=.25, import_price=.3, export_price=.1)
    assert plant.step(action_kw=2, **base)["energy_kwh"] == 5
    result = plant.step(action_kw=-2, **base)
    assert result["energy_kwh"] == pytest.approx(5.45)
    assert result["import_kwh"] == .75
    result = plant.step(action_kw=0, **base)
    assert result["energy_kwh"] == pytest.approx(4.825)
    assert result["export_kwh"] == .25
    result = plant.step(action_kw=1, available=False, **base)
    assert result["energy_kwh"] == pytest.approx(4.825)
    with pytest.raises(PlantError):
        Plant(Parameters(10, 5, 5, .95, .95, .1), .1).step(action_kw=-1, **base)


def test_candidate_actions_produce_distinct_trajectories_and_scores():
    p = Parameters(10, 5, 5, .95, .95, .1, uncertainty_kwh=.2)
    inputs = [dict(load_kw=1, solar_kw=0, hours=.25, import_price=price, export_price=.1) for price in (.1, .5)]
    result = paired(p, .5, inputs, [0, 0], [1, -1])
    assert result["cost_delta"] < 0
    assert result["terminal_delta_kwh"] < 0
    assert sensitivity(p, .5, inputs, [0, 0], [1, -1])["arms"] == 9


def test_calibration_fit_withheld_and_out_of_range():
    training = [dict(before=5, after=5.45, power=2, hours=.25)]*2 + [dict(before=5, after=4.375, power=-2, hours=.25)]*2
    training = [{**row, "at": index} for index,row in enumerate(training)]
    withheld = [{**row, "at": index+100} for index,row in enumerate(training[::2])]
    fitted, result = calibrate(Parameters(10, 5, 5, .95, .95, .1), training, withheld)
    assert fitted.charge_efficiency == pytest.approx(.9)
    assert fitted.discharge_efficiency == pytest.approx(.8)
    assert result["valid"]
    wrong = [{**row, "after": row["after"] + .2} for row in withheld]
    assert not calibrate(Parameters(10, 5, 5, .95, .95, .1), training, wrong)[1]["valid"]
    with pytest.raises(PlantError):
        calibrate(Parameters(10, 5, 5, .95, .95, .1), training, [{**row, "power": 20} for row in withheld])


def test_seven_day_gate_counts_only_eligible_complete_installations(tmp_path):
    secret = b'x'*32
    c = Campaign(*([char*64 for char in '12345']+[hashlib.sha256(secret).hexdigest()]), .01, 2, pair='a'*64)
    sites = {}
    for index in range(100):
        site = f'{index:064x}'
        sites.setdefault(holdout(secret, c, site), site)
    ledger = HoldoutLedger(tmp_path/'cohorts.sqlite')
    rows = [dict(job=f'{day*2+index:064x}', campaign=c.digest, pair=c.pair, scorer=c.scorer, schema=c.schema,
        installation=site, completed_at=100+day*86400, purge='confirmed', status='pass', cost_delta=0, violations=0,
        coverage='complete', cohort=cohort, attempts=1) for index,(cohort,site) in enumerate(sites.items()) for day in range(7)]
    reservations = [dict(job=row['job'], installation=row['installation'], created_at=row['completed_at'], campaign=c.digest) for row in rows]
    kwargs = dict(start=100, now=100+7*86400, reservations=reservations, holdout_secret=secret, ledger=ledger)
    result = seven_day(c, rows, **kwargs)
    assert result['status'] == 'pass' and result['logical_installations'] == 2 and result['unique_households'] is None
    assert seven_day(c, rows[:-1], **kwargs)['status'] == 'pending'
    invalid = [{**row,'status':'invalid'} if row['cohort']=='holdout' else row for row in rows]
    assert seven_day(c, invalid, **kwargs)['status'] == 'pending'
    assert seven_day(c, rows, start=100, now=100+7*86400)['status'] == 'pending'
    uncertain=[{**row,'cost_delta':-.008 if row['cohort']=='holdout' else -.004} for row in rows]
    assert seven_day(c,uncertain,**kwargs)['status']!='pass'
    failed = [{**row,'status':'fail'} if index == 0 else {**row,'status':'invalid'} for index,row in enumerate(rows)]
    assert seven_day(c, failed, **kwargs)['status'] == 'fail'
    for changed in ({'schedule':[]}, {'purge':'unconfirmed'}, {'cohort':'wrong'}):
        with pytest.raises(CampaignError):
            seven_day(c, [{**rows[0], **changed}], **kwargs)
    with pytest.raises(CampaignError):
        ledger.consume(Campaign(*[char*64 for char in '1b3456'], .01, 2, pair='a'*64), secret)
    with pytest.raises(CampaignError):
        holdout(secret, c, 'installation', consumed=True)


def test_pending_measured_acceptance_never_passes_fleet_gate():
    result = acceptance(software='passed', protected_main='passed', codex='passed', claude='passed', runner='pending',
                        consent='pending', calibration='pending', observation='pending')
    assert result['fleet_gate'] == 'pending'
