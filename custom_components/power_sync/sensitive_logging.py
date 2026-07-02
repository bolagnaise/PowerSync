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
    """Redact string log args while preserving non-string formatting types."""
    if not isinstance(arg, str):
        return arg

    obfuscated = obfuscate_string(arg)
    if obfuscated != arg:
        return obfuscated
    return arg
