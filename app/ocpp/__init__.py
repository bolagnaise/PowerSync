# app/ocpp/__init__.py
"""
OCPP (Open Charge Point Protocol) module for EV charger control.

Implements an OCPP 1.6J central system server that runs in a background thread,
allowing OCPP-compliant chargers to connect and be controlled via automations.
"""

from .server import OCPPServer, get_ocpp_server

__all__ = ['OCPPServer', 'get_ocpp_server']
