"""Local Tesla Powerwall control for PowerSync.

Provides direct LAN communication with the Powerwall Gateway, bypassing the
Tesla cloud. Supports two gateway generations:

- Powerwall 2: plain REST at ``https://<gateway>/api/...`` with Bearer-token
  auth from the customer login endpoint.
- Powerwall 3: RSA-4096 signed protobuf at ``/tedapi/v1r`` after registering
  an authorized-client public key with Tesla Fleet API.

The RSA public key must be pre-registered via the pairing flow (see
``pairing.py``), which requires the user to physically toggle the gateway's
DC isolator as Tesla's physical-presence proof.

Protobuf schema and signing approach derived from jasonacox/pypowerwall
(MIT License). Compiled descriptor ships in ``tedapi_combined_pb2.py``.
"""

from .client import PowerwallLocalClient, PowerwallVersion
from .exceptions import (
    PowerwallAuthError,
    PowerwallLocalError,
    PowerwallPairingError,
    PowerwallUnreachableError,
)
from .pairing import PairingState, PairingStatus, PowerwallPairingManager

__all__ = [
    "PairingState",
    "PairingStatus",
    "PowerwallAuthError",
    "PowerwallLocalClient",
    "PowerwallLocalError",
    "PowerwallPairingError",
    "PowerwallPairingManager",
    "PowerwallUnreachableError",
    "PowerwallVersion",
]
