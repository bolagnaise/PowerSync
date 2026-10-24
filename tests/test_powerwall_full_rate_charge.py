"""Native Max honors the public service intent and enforced site constraints."""
import importlib.util
from pathlib import Path
import sys
import pytest

path=Path(__file__).parents[1]/'custom_components/power_sync/powerwall_local/full_rate_charge.py'
spec=importlib.util.spec_from_file_location('native_max_policy_test',path)
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)


def eligible(**changes):
    values=dict(source='user',requested_power_w=0,grid_import_target_w=None,
        planning_charge_max_w=14700,software_import_max_w=15000,
        native_import_max_w=14900,native_import_known=True,pending_ev_w=0,
        native_ev_active=False,grid_charging_allowed=True,grid_connected=True,
        soc=70,soc_cap=100,evidence_fresh=True,control_permitted=True)
    return m.native_max_charge_eligibility(**dict(values,**changes))


@pytest.mark.parametrize('source',['user','manual','automation','unknown'])
@pytest.mark.parametrize('power_request',[None,0,0.0])
def test_explicit_native_max_needs_no_unavailable_charge_rating(source,power_request):
    result=eligible(source=source,requested_power_w=power_request)
    assert result.eligible and result.native_import_max_w==14900
    assert eligible(planning_charge_max_w=5000).eligible


@pytest.mark.parametrize('changes,reason',[
    ({'source':'optimizer'},'native_max_requires_explicit_max_request'),
    ({'requested_power_w':14700},'native_max_requires_explicit_max_request'),
    ({'requested_power_w':True},'native_max_requires_explicit_max_request'),
    ({'grid_import_target_w':14900},'grid_target_requires_local_control'),
    ({'planning_charge_max_w':0},'configured_charge_disabled'),
    ({'planning_charge_max_w':None},'configured_charge_disabled'),
    ({'software_import_max_w':12000},'software_import_limit_requires_local_control'),
    ({'software_import_max_w':False},'software_import_limit_invalid'),
    ({'native_import_known':False},'native_limit_evidence_unavailable'),
    ({'native_import_known':'true'},'native_limit_evidence_unavailable'),
    ({'native_import_max_w':0},'native_import_limit_blocks_charge'),
    ({'native_import_max_w':float('nan')},'native_import_limit_blocks_charge'),
    ({'native_import_max_w':None},'software_import_limit_requires_local_control'),
    ({'pending_ev_w':1000},'ev_demand_requires_local_control'),
    ({'pending_ev_w':float('nan')},'ev_demand_requires_local_control'),
    ({'native_ev_active':True},'ev_demand_requires_local_control'),
    ({'native_ev_active':None},'ev_demand_requires_local_control'),
    ({'soc':100},'charge_soc_limit_reached_or_unknown'),
    ({'soc_cap':60},'charge_soc_limit_reached_or_unknown'),
    ({'grid_connected':False},'grid_charging_not_permitted'),
    ({'grid_charging_allowed':False},'grid_charging_not_permitted'),
    ({'control_permitted':False},'native_max_control_not_permitted'),
    ({'evidence_fresh':False},'native_limit_evidence_unavailable'),
])
def test_native_max_cannot_erase_current_permissions_or_site_limits(changes,reason):
    result=eligible(**changes)
    assert not result.eligible and result.reason==reason


def test_explicit_unlimited_import_and_configured_zero_setting():
    assert eligible(native_import_max_w=None,software_import_max_w=None).eligible
    assert eligible(native_import_max_w=None,software_import_max_w=0).eligible
