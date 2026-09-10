"""Shared helpers for redacting sensitive values from logs."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


_VIN_TOKEN_RE = re.compile(
    r"(?<![A-HJ-NPR-Z0-9])"
    r"(?=[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9]))"
    r"(?=[A-HJ-NPR-Z0-9]*\d)"
    r"(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])"
    r"([A-HJ-NPR-Z0-9]{17})"
    r"(?![A-HJ-NPR-Z0-9])",
    re.IGNORECASE,
)


def obfuscate_vin_tokens(text: str, obfuscate: Callable[[str], str]) -> str:
    """Mask standalone VIN tokens wherever they appear in a log message."""
    return _VIN_TOKEN_RE.sub(lambda match: obfuscate(match.group(1)), text)


def obfuscate_log_arg(
    arg: Any,
    obfuscate_string: Callable[[str], str],
) -> Any:
    """Redact strings nested in log args while preserving formatting types."""
    if isinstance(arg, str):
        return obfuscate_string(arg)
    if isinstance(arg, dict):
        return {
            obfuscate_log_arg(key, obfuscate_string): obfuscate_log_arg(value, obfuscate_string)
            for key, value in arg.items()
        }
    if isinstance(arg, tuple):
        return tuple(obfuscate_log_arg(value, obfuscate_string) for value in arg)
    if isinstance(arg, list):
        return [obfuscate_log_arg(value, obfuscate_string) for value in arg]
    return arg
