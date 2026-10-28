"""Explicit permission evidence from both Tesla response shapes."""
import importlib
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1] / 'custom_components/power_sync'


@pytest.fixture
def permissions(monkeypatch):
    package = ModuleType('export_permission_probe')
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    return importlib.import_module(package.__name__ + '.tesla_export_permissions')


@pytest.mark.parametrize('rule', ['never', 'pv_only', 'battery_ok'])
def test_owner_site_shapes_report_same_explicit_rule(permissions, rule):
    parse = permissions.tesla_export_rule_from_site_info
    assert parse({'customer_preferred_export_rule': rule}) == rule
    assert parse({'components': {'customer_preferred_export_rule': rule}}) == rule


@pytest.mark.parametrize('missing', [None])
def test_null_or_malformed_components_cannot_hide_valid_top_level(permissions, missing):
    assert permissions.tesla_export_rule_from_site_info({
        'components': {'customer_preferred_export_rule': missing},
        'customer_preferred_export_rule': 'pv_only'}) == 'pv_only'


@pytest.mark.parametrize('invalid', ['', 'invalid', True, 1, {}, []])
def test_invalid_nested_value_is_not_silently_reclassified_as_absent(permissions, invalid):
    assert permissions.tesla_export_rule_from_site_info({
        'components': {'customer_preferred_export_rule': invalid},
        'customer_preferred_export_rule': 'pv_only'}) is None


def test_conflicting_recognized_cloud_paths_remain_unknown(permissions):
    assert permissions.read_export_rule({'customer_preferred_export_rule': 'never',
        'components': {'customer_preferred_export_rule': 'battery_ok'}}).status == 'conflict'


@pytest.mark.parametrize('payload', [{}, {'components': {}},
    {'components': {'non_export_configured': False}},
    {'customer_preferred_export_rule': None},
    {'customer_preferred_export_rule': 'invalid'}])
def test_absence_never_becomes_guessed_battery_export(permissions, payload):
    assert permissions.tesla_export_rule_from_site_info(payload) is None


def test_explicit_non_export_true_preserves_never(permissions):
    assert permissions.tesla_export_rule_from_site_info({
        'components': {'non_export_configured': True}}) == 'never'
