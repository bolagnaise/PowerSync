"""Repo-root pytest configuration.

Fail fast with an actionable message when the test suite is run under a Python
that is too old. PowerSync (like Home Assistant, which ships Python 3.12/3.13)
uses PEP 604 ``X | None`` syntax that only evaluates on Python 3.10+. On older
interpreters the test modules raise a cryptic ``TypeError: unsupported operand
type(s) for |`` during collection — this guard turns that wall of errors into a
single clear instruction.
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 10)

if sys.version_info < MIN_PYTHON:
    current = ".".join(str(part) for part in sys.version_info[:3])
    raise RuntimeError(
        f"PowerSync tests require Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} "
        f"(Home Assistant runs 3.12/3.13); you are using {current}. "
        "Run the suite with a newer interpreter, e.g. `python3.12 -m pytest`."
    )
