"""Construct internal service calls across Home Assistant constructor versions."""
from __future__ import annotations

from inspect import signature

from homeassistant.core import ServiceCall


def make_service_call(hass, domain, service, data, context=None, return_response=False):
    """Bind control data by name, including HA only when its API requires it."""
    kwargs = {
        "domain": domain,
        "service": service,
        "data": data,
        "context": context,
        "return_response": return_response,
    }
    if "hass" in signature(ServiceCall).parameters:
        kwargs["hass"] = hass
    return ServiceCall(**kwargs)
