"""Internal calls keep control data under old and HA 2026.10 constructors."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest


class PreviousServiceCall:
    def __init__(self, domain, service, data=None, context=None, return_response=False):
        self.domain, self.service = domain, service
        self.data = data if data is not None else {}
        self.context, self.return_response = context, return_response


class CurrentServiceCall:
    def __init__(self, hass, domain, service, data=None, context=None, return_response=False):
        self.hass, self.domain, self.service = hass, domain, service
        self.data = data if data is not None else {}
        self.context, self.return_response = context, return_response


@pytest.fixture
def service_calls(monkeypatch):
    # Stub the import, then replace this isolated module's constructor binding.
    # Never mutate Home Assistant's global ServiceCall class or another test's stub.
    parent, core = ModuleType("homeassistant"), ModuleType("homeassistant.core")
    parent.__path__ = []
    core.ServiceCall = object
    parent.core = core
    monkeypatch.setitem(sys.modules, "homeassistant", parent)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    path = Path(__file__).parents[1] / "custom_components/power_sync/service_calls.py"
    spec = importlib.util.spec_from_file_location("service_call_compat_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("constructor", [PreviousServiceCall, CurrentServiceCall], ids=["previous-ha", "ha-2026.10"])
@pytest.mark.parametrize("return_response", [False, True])
def test_factory_preserves_exact_control_data_context_and_response_intent(
        monkeypatch, service_calls, constructor, return_response):
    monkeypatch.setattr(service_calls, "ServiceCall", constructor)
    hass, context, transition = object(), NS(user_id="test-user"), object()
    data = {"percent": 0, "source": "hold_soc", "entry_id": "entry-one",
            "_reserve_restore_target": 0, "_hold_soc_transition_token": transition,
            "_tesla_restore_generation": 41, "_skip_backup_reserve_restore": False}
    call = service_calls.make_service_call(hass, "power_sync", "set_backup_reserve", data,
                                           context=context, return_response=return_response)
    assert call.domain == "power_sync" and call.service == "set_backup_reserve"
    assert call.data is data and call.data["percent"] == 0
    assert call.data["_hold_soc_transition_token"] is transition
    assert call.context is context and call.return_response is return_response
    if constructor is CurrentServiceCall:
        assert call.hass is hass
    else:
        assert not hasattr(call, "hass")


@pytest.mark.parametrize("constructor", [PreviousServiceCall, CurrentServiceCall], ids=["previous-ha", "ha-2026.10"])
def test_default_context_and_empty_payload_are_not_shifted(monkeypatch, service_calls, constructor):
    monkeypatch.setattr(service_calls, "ServiceCall", constructor)
    data = {}
    call = service_calls.make_service_call(object(), "power_sync", "restore_normal", data)
    assert (call.domain, call.service) == ("power_sync", "restore_normal")
    assert call.data is data and call.context is None and call.return_response is False


def test_current_constructor_positional_misbinding_is_prevented(monkeypatch, service_calls):
    payload = {"percent": 0, "source": "hold_soc"}
    # This is the previously used call shape: the payload becomes `service`
    # and a real handler receives empty data, losing the required percentage.
    old_shape = CurrentServiceCall("power_sync", "set_backup_reserve", payload)
    assert old_shape.data == {} and old_shape.service is payload
    monkeypatch.setattr(service_calls, "ServiceCall", CurrentServiceCall)
    corrected = service_calls.make_service_call(object(), "power_sync", "set_backup_reserve", payload)
    assert corrected.service == "set_backup_reserve"
    assert corrected.data["percent"] == 0 and corrected.data["source"] == "hold_soc"


def test_constructor_supports_keyword_only_hass(monkeypatch, service_calls):
    class KeywordOnlyCall:
        def __init__(self, *, hass, domain, service, data, context, return_response):
            self.values = hass, domain, service, data, context, return_response

    monkeypatch.setattr(service_calls, "ServiceCall", KeywordOnlyCall)
    hass, data, context = object(), {"_force_restore": False}, object()
    result = service_calls.make_service_call(hass, "power_sync", "restore_normal", data, context, True)
    assert result.values == (hass, "power_sync", "restore_normal", data, context, True)


def test_constructor_type_error_is_not_retried(monkeypatch, service_calls):
    attempts = []

    class RejectingCall:
        def __init__(self, hass, domain, service, data=None, context=None, return_response=False):
            attempts.append(data)
            raise TypeError("constructor failed after recording its input")

    monkeypatch.setattr(service_calls, "ServiceCall", RejectingCall)
    data = {"source": "user", "_force_restore": False}
    with pytest.raises(TypeError, match="constructor failed after recording its input"):
        service_calls.make_service_call(object(), "power_sync", "restore_normal", data)
    assert attempts == [data]
