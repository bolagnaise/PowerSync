"""Regression coverage for PW-8: curtailment restore branches must respect a
manual export-rule override instead of hardcoding 'battery_ok'.

Both `handle_solar_curtailment_check` (REST 5-min poll) and
`handle_solar_curtailment_with_websocket_data` (fast-reaction WebSocket path)
correctly compute `restore_rule` (the manual override's rule, or
'battery_ok' when no override is active) and POST it to Tesla. But the
verification check, the success log, and the cached-state write downstream
of that POST hardcoded the literal 'battery_ok' instead of using
`restore_rule` -- so a user's manual 'never' override got silently
overwritten in PowerSync's own cache (and a spurious verification warning
fired) even though the correct rule was actually sent to Tesla.

These tests exec the extracted restore branch against fake session/hass
objects (the AGENTS.md-sanctioned pattern from
tests/test_sungrow_curtailment_runtime.py) and assert on the actual
runtime calls made -- not just source-text substrings.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace

import aiohttp


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            for child in node.body:
                if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef)) and child.name == name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{name} not found")


def _restore_branch_source(function_name: str) -> str:
    """Isolate the 'restore from curtailment' branch of a handler and wrap
    it as a standalone async function taking its free variables as args."""
    handler = _function_source(function_name)
    start = handler.index("if current_export_rule == \"never\":")
    end = handler.index(
        "except Exception as err:\n                        _LOGGER.error(f\"Error restoring from curtailment: {err}\")"
    )
    end = handler.index("\n", end) + 1  # past "except Exception as err:"
    end = handler.index("\n", end) + 1  # past the _LOGGER.error(...) line
    end = handler.index("\n", end) + 1  # past the "return" line
    branch = handler[start:end]
    dedented = textwrap.dedent(
        "async def _restore(session, hass, entry, DOMAIN, aiohttp, export_earnings,\n"
        "                    apply_inverter_curtailment, update_cached_export_rule, _LOGGER,\n"
        "                    api_base_url, headers, CONF_TESLA_ENERGY_SITE_ID,\n"
        "                    current_export_rule):\n"
    ) + textwrap.indent(textwrap.dedent(branch), "    ")
    return dedented


class _FakeResponse:
    def __init__(self, status=200, json_body=None):
        self.status = status
        self._json_body = json_body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json_body

    async def text(self):
        return "error"


class _FakeSession:
    """Fake aiohttp session: POST accepts the rule, GET read-back reports
    whatever rule was actually set (mirrors real Tesla API behavior)."""

    def __init__(self):
        self.posted_rule = None

    def post(self, url, headers=None, json=None, timeout=None):
        self.posted_rule = json["customer_preferred_export_rule"]
        return _FakeResponse(status=200, json_body={"response": {"result": True}})

    def get(self, url, headers=None, timeout=None):
        return _FakeResponse(
            status=200,
            json_body={
                "response": {
                    "customer_preferred_export_rule": self.posted_rule,
                }
            },
        )


def _make_namespace():
    return {
        "aiohttp": aiohttp,
    }


def _run_restore_branch(function_name: str, manual_override: bool, manual_rule: str | None):
    branch_source = _restore_branch_source(function_name)
    namespace = _make_namespace()
    exec(branch_source, namespace)
    restore_fn = namespace["_restore"]

    session = _FakeSession()
    entry_data = {
        "manual_export_override": manual_override,
        "manual_export_rule": manual_rule,
    }
    hass = SimpleNamespace(data={"power_sync": {"entry1": entry_data}})
    entry = SimpleNamespace(
        entry_id="entry1",
        data={"tesla_energy_site_id": "site1"},
    )

    warnings = []
    infos = []
    cached_calls = []
    inverter_calls = []

    _logger = SimpleNamespace(
        info=lambda msg: infos.append(msg),
        warning=lambda msg: warnings.append(msg),
        error=lambda msg: None,
        debug=lambda msg: None,
    )

    async def update_cached_export_rule(rule):
        cached_calls.append(rule)

    async def apply_inverter_curtailment(curtail):
        inverter_calls.append(curtail)

    asyncio.run(
        restore_fn(
            session=session,
            hass=hass,
            entry=entry,
            DOMAIN="power_sync",
            aiohttp=aiohttp,
            export_earnings=2.5,
            apply_inverter_curtailment=apply_inverter_curtailment,
            update_cached_export_rule=update_cached_export_rule,
            _LOGGER=_logger,
            api_base_url="https://owner-api.teslamotors.com",
            headers={},
            CONF_TESLA_ENERGY_SITE_ID="tesla_energy_site_id",
            current_export_rule="never",
        )
    )

    return SimpleNamespace(
        posted_rule=session.posted_rule,
        cached_calls=cached_calls,
        warnings=warnings,
        infos=infos,
    )


def test_rest_restore_respects_manual_never_override():
    result = _run_restore_branch(
        "handle_solar_curtailment_check", manual_override=True, manual_rule="never"
    )

    assert result.posted_rule == "never"
    assert result.cached_calls == ["never"], (
        "cache must reflect the actually-posted manual rule, not a hardcoded battery_ok"
    )
    assert not any("RESTORE VERIFICATION FAILED" in w for w in result.warnings), (
        "read-back legitimately shows 'never' for a manual override; must not be flagged"
    )
    assert any("'never' → 'never'" in i for i in result.infos)


def test_rest_restore_defaults_to_battery_ok_without_override():
    result = _run_restore_branch(
        "handle_solar_curtailment_check", manual_override=False, manual_rule=None
    )

    assert result.posted_rule == "battery_ok"
    assert result.cached_calls == ["battery_ok"]
    assert not any("RESTORE VERIFICATION FAILED" in w for w in result.warnings)


def test_websocket_restore_respects_manual_never_override():
    result = _run_restore_branch(
        "handle_solar_curtailment_with_websocket_data",
        manual_override=True,
        manual_rule="never",
    )

    assert result.posted_rule == "never"
    assert result.cached_calls == ["never"], (
        "cache must reflect the actually-posted manual rule, not a hardcoded battery_ok"
    )
    assert not any("RESTORE VERIFICATION FAILED" in w for w in result.warnings), (
        "the websocket restore branch is a byte-for-byte sibling of the REST branch "
        "and must not regress independently"
    )


def test_websocket_restore_defaults_to_battery_ok_without_override():
    result = _run_restore_branch(
        "handle_solar_curtailment_with_websocket_data",
        manual_override=False,
        manual_rule=None,
    )

    assert result.posted_rule == "battery_ok"
    assert result.cached_calls == ["battery_ok"]
    assert not any("RESTORE VERIFICATION FAILED" in w for w in result.warnings)
