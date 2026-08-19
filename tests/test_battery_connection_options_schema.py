"""Regression tests for the Battery connection method & sensors options step.

A `vol.Optional(key, default=...)` default is validated by voluptuous even when
the frontend omits the field, and an `EntitySelector` rejects both "" and None.
A blank default therefore made the whole options step unsubmittable with
"Entity is neither a valid entity ID nor a valid UUID" on every Sungrow install
that had never stored an anchor sensor.  Home Assistant is not installed in the
unit-test environment, so the schema branch is extracted from source (the
AST pattern used elsewhere in this suite) and executed against stubs.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace


CONFIG_FLOW = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "config_flow.py"
)

_NO_DEFAULT = object()


class _Optional:
    """Stub of vol.Optional that records whether a default was attached."""

    def __init__(self, key, default=_NO_DEFAULT):
        self.key = key
        self.default = default

    def __hash__(self):
        return hash(self.key)


def _entity_id_or_uuid(value):
    """Mirror cv.entity_id_or_uuid's rejection of blank/None entity ids."""
    text = value if isinstance(value, str) else ""
    if not text or "." not in text:
        raise ValueError(
            f"Entity {value if value is not None else 'None'} is neither a "
            "valid entity ID nor a valid UUID"
        )
    return text


def _anchor_branch_source(function_name: str) -> str:
    """Return the Sungrow anchor-field branch of the named flow step."""
    source_text = CONFIG_FLOW.read_text()
    tree = ast.parse(source_text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != function_name:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            test = ast.get_source_segment(source_text, inner.test) or ""
            if "BATTERY_SYSTEM_SUNGROW" not in test:
                continue
            branch = ast.get_source_segment(source_text, inner) or ""
            if "ANCHOR_ENTITY" in branch:
                return textwrap.dedent(branch)
    raise AssertionError(f"anchor branch not found in {function_name}")


def _build_anchor_key(function_name: str, stored):
    """Execute the extracted branch and return the recorded schema key."""
    schema_fields: dict = {}
    namespace = {
        "battery_system": "sungrow",
        "BATTERY_SYSTEM_SUNGROW": "sungrow",
        "CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY": "battery_integration_anchor_entity",
        "schema_fields": schema_fields,
        "vol": SimpleNamespace(Optional=_Optional),
        "EntitySelector": lambda config: config,
        "EntitySelectorConfig": lambda **kwargs: kwargs,
        "self": SimpleNamespace(
            _get_option=lambda key, default=None: (
                default if stored is _NO_DEFAULT else stored
            )
        ),
    }
    exec(compile(_anchor_branch_source(function_name), "<anchor>", "exec"), namespace)
    assert len(schema_fields) == 1
    return next(iter(schema_fields))


def test_options_step_omits_the_default_until_a_real_anchor_is_stored():
    for stored in (_NO_DEFAULT, None, "", "   "):
        key = _build_anchor_key("async_step_battery_connection_profile", stored)
        assert key.default is _NO_DEFAULT, stored
        # With no default, an omitted field never reaches the selector at all.


def test_options_step_still_round_trips_a_stored_anchor_entity():
    key = _build_anchor_key(
        "async_step_battery_connection_profile",
        "sensor.sungrow_battery_level",
    )

    assert key.default == "sensor.sungrow_battery_level"
    assert _entity_id_or_uuid(key.default) == "sensor.sungrow_battery_level"


def test_setup_step_anchor_field_shape_is_unchanged():
    """The initial-setup twin already worked; keep it that way."""
    key = _build_anchor_key(
        "async_step_battery_connection_profile_setup",
        _NO_DEFAULT,
    )

    assert key.default is _NO_DEFAULT


def test_blank_anchor_defaults_are_what_the_selector_rejects():
    """Document the failure the missing default protects against."""
    for blank in ("", None):
        try:
            _entity_id_or_uuid(blank)
        except ValueError as error:
            assert "is neither a valid entity ID nor a valid UUID" in str(error)
        else:  # pragma: no cover - guard against a silently loosened stub
            raise AssertionError(f"{blank!r} must be rejected")
