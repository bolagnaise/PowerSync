"""Powerwall gateway host validation and normalization."""

from __future__ import annotations

from ipaddress import IPv6Address
from urllib.parse import urlsplit


def normalize_powerwall_gateway_host(value: object) -> str:
    """Return a validated bare host for Powerwall's HTTPS endpoints.

    Empty input intentionally clears local access. Non-empty input may be a
    bare host with an optional port, or an HTTP(S) URL whose path is empty or
    ``/``. Ambiguous authorities are rejected instead of silently contacting a
    different endpoint.
    """
    if value is None:
        raw = ""
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise ValueError("Powerwall gateway address must be a string")
    if not raw:
        return ""
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("Powerwall gateway address contains whitespace")

    if "://" not in raw and raw.count(":") >= 2 and not raw.startswith("["):
        try:
            return f"[{IPv6Address(raw)}]"
        except ValueError:
            pass

    has_scheme = "://" in raw
    parsed = urlsplit(raw if has_scheme else f"//{raw}")
    if has_scheme and parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Powerwall gateway scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Powerwall gateway address must not include user information")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Powerwall gateway address must not include a path or query")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Powerwall gateway host is missing")
    if any(
        character.isspace()
        or ord(character) < 32
        or character in "/?#@\\"
        for character in hostname
    ):
        raise ValueError("Powerwall gateway host contains invalid characters")

    try:
        port = parsed.port
    except ValueError as err:
        raise ValueError("Powerwall gateway port is invalid") from err
    if port == 0:
        raise ValueError("Powerwall gateway port must be between 1 and 65535")

    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}" if port is not None else hostname
