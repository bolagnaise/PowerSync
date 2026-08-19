"""Regression coverage for provider-config Auto Sync responses."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import textwrap


INIT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def _handler_globals() -> dict:
    """Real module globals the extracted handler resolves at call time.

    ``const``, ``currency`` and ``zerohero`` are stdlib-only, so they load
    standalone without the Home Assistant import chain.  Loading them beats
    hand-listing names: the handler grows references over time, and anything
    missing from this namespace surfaces as a NameError swallowed by the
    view's own except clause -- an opaque HTTP 500 rather than a clear failure.

    Deliberately not registered in ``sys.modules``: these are for this
    namespace only and must not become the tree another test file imports.
    """
    globals_: dict = {}
    for module_name in ("const", "currency", "zerohero"):
        path = INIT_PATH.parent / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(
            f"_ps_standalone_{module_name}", path
        )
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolves sys.modules[cls.__module__] while building a
        # frozen class, so the module has to be registered for the exec.  Drop
        # it straight after: this tree is for this namespace only and must not
        # become something another test file can import.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        globals_.update(
            {
                name: value
                for name, value in vars(module).items()
                if not name.startswith("__")
            }
        )
    return globals_


def _provider_config_get():
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "ProviderConfigView":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "get":
                    method_source = ast.get_source_segment(source, child)
                    assert method_source is not None
                    break
            else:
                raise AssertionError("ProviderConfigView.get not found")
            break
    else:
        raise AssertionError("ProviderConfigView not found")

    class _Logger:
        def info(self, *_args, **_kwargs):
            pass

        warning = info
        error = info

    def _json_response(payload, status=200):
        return SimpleNamespace(payload=payload, status=status)

    web = SimpleNamespace(Request=object, Response=object, json_response=_json_response)
    namespace = {
        "web": web,
        "_LOGGER": _Logger(),
        **_handler_globals(),
    }
    exec(textwrap.dedent(method_source), namespace)
    return namespace["get"]


def test_octopus_provider_config_returns_persisted_auto_sync_false():
    """Every provider exposing Auto Sync must return its persisted false value."""

    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"battery_system": "tesla"},
        options={
            "electricity_provider": "octopus",
            "auto_sync_enabled": False,
        },
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _domain: [entry]),
        data={"power_sync": {"entry-1": {}}},
    )

    response = asyncio.run(_provider_config_get()(SimpleNamespace(_hass=hass), None))

    assert response.status == 200
    assert response.payload["success"] is True
    assert response.payload["electricity_provider"] == "octopus"
    assert response.payload["config"]["auto_sync"] is False
