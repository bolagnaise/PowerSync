"""Exceptions raised by the powerwall_local package."""


class PowerwallLocalError(Exception):
    """Base error for local Powerwall operations."""


class PowerwallUnreachableError(PowerwallLocalError):
    """Gateway is not reachable on the network."""


class PowerwallAuthError(PowerwallLocalError):
    """Authentication to the gateway failed (bad password or missing key)."""


class PowerwallPairingError(PowerwallLocalError):
    """RSA key pairing / registration failed."""


class PowerwallSignatureError(PowerwallLocalError):
    """Gateway rejected an RSA signature (unknown key, expired, etc)."""
