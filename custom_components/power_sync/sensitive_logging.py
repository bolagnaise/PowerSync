"""Shared helpers for redacting sensitive values from logs."""

from __future__ import annotations

import re
import logging
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


class _VinLogFilter(logging.Filter):
    """Redact at the emitting logger; parent logger filters do not propagate."""

    powersync_vin_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        def redact(text: str) -> str:
            return obfuscate_vin_tokens(
                text, lambda value: f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
            )

        record.msg = redact(str(record.msg))
        record.args = obfuscate_log_arg(record.args, redact)
        return True


def install_vin_log_filter(logger: logging.Logger) -> None:
    """Install one VIN filter, including across integration module reloads."""
    if not any(getattr(item, "powersync_vin_filter", False) for item in logger.filters):
        logger.addFilter(_VinLogFilter())
