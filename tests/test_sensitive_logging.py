"""Tests for shared log redaction helpers."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import logging
from pathlib import Path
import re
import sys
import textwrap
import types
from functools import lru_cache
from typing import Any


_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_sync"
    / "sensitive_logging.py"
)
_SPEC = importlib.util.spec_from_file_location("power_sync_sensitive_logging", _MODULE_PATH)
assert _SPEC is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
obfuscate_vin_tokens = _MODULE.obfuscate_vin_tokens
obfuscate_log_arg = _MODULE.obfuscate_log_arg


VIN = "LRWYHCFS3PC901374"
MASKED_VIN = "LRWY*********1374"


def _mask(value: str) -> str:
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


@lru_cache(maxsize=None)
def _load_sensitive_filter_class(source_path_string: str):
    source_path = Path(source_path_string)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SensitiveDataFilter"
    )
    namespace = {
        "Any": Any,
        "logging": logging,
        "obfuscate_log_arg": obfuscate_log_arg,
        "obfuscate_vin_tokens": obfuscate_vin_tokens,
        "re": re,
    }
    exec(
        compile(
            ast.Module(body=[class_node], type_ignores=[]),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    return namespace["SensitiveDataFilter"]


def _load_sensitive_filter(source_path: Path):
    return _load_sensitive_filter_class(str(source_path))()


def _render_filtered_log(filter_instance, message: str, *args: Any) -> str:
    record = logging.LogRecord(
        name="power_sync.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )
    assert filter_instance.filter(record)
    return record.getMessage()


@lru_cache(maxsize=None)
def _log_statement_for(source_path_string: str, phrase: str) -> ast.Expr:
    source_path = Path(source_path_string)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "info"
        and phrase
        in " ".join(
            constant.value
            for constant in ast.walk(node.value)
            if isinstance(constant, ast.Constant)
            and isinstance(constant.value, str)
        )
    )
    return node


def _execute_log_statement(
    source_path: Path,
    phrase: str,
    filter_instance,
    site_id: str | int,
) -> str:
    node = _log_statement_for(str(source_path), phrase)
    logger = logging.getLogger(f"power_sync.test.{phrase}")
    logger.handlers = []
    logger.filters = []
    logger.addFilter(filter_instance)
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    namespace = {
        "_LOGGER": logger,
        "CONF_TESLA_ENERGY_SITE_ID": "site_id",
        "SensitiveDataFilter": type(filter_instance),
        "entry": types.SimpleNamespace(data={"site_id": site_id}),
        "self": types.SimpleNamespace(
            api_base_url="https://example.test",
            site_id=site_id,
        ),
        "site_id": site_id,
    }
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"),
        namespace,
    )
    return stream.getvalue().strip()


def test_tesla_site_id_logs_mask_all_known_emissions() -> None:
    """Every known Tesla site-ID INFO emission masks short and deferred IDs."""
    repo_root = Path(__file__).resolve().parents[1]
    coordinator_path = repo_root / "custom_components" / "power_sync" / "coordinator.py"
    init_path = repo_root / "custom_components" / "power_sync" / "__init__.py"
    emissions = (
        (coordinator_path, "PowerSync.cc proxy for site", "TeslaEnergyCoordinator initialized with PowerSync.cc proxy for site"),
        (coordinator_path, "Fleet API for site", "TeslaEnergyCoordinator initialized with Fleet API for site"),
        (coordinator_path, "Teslemetry for site", "TeslaEnergyCoordinator initialized with Teslemetry for site"),
        (coordinator_path, "Probing Tesla Energy Site capabilities", "Probing Tesla Energy Site capabilities for site"),
        (init_path, "Detected Tesla Fleet integration", "Detected Tesla Fleet integration - using Fleet API tokens for site"),
        (init_path, "Using PowerSync.cc cloud proxy", "Using PowerSync.cc cloud proxy for site"),
        (init_path, "Using Teslemetry API", "Using Teslemetry API for site"),
    )
    site_cases = (
        (12345678, "********"),
        ("1234567890123", "1234*****0123"),
        ("123456789012345", "1234*******2345"),
    )

    for source_path, phrase, expected_prefix in emissions:
        for site_id, expected_mask in site_cases:
            result = _execute_log_statement(
                source_path,
                phrase,
                _load_sensitive_filter(source_path),
                site_id,
            )
            assert result.startswith(expected_prefix)
            assert expected_mask in result
            assert str(site_id) not in result


def test_sensitive_filter_preserves_unrelated_numeric_formatting() -> None:
    """Site-ID redaction must not change unrelated typed log arguments."""
    repo_root = Path(__file__).resolve().parents[1]
    source_path = repo_root / "custom_components" / "power_sync" / "coordinator.py"
    filter_instance = _load_sensitive_filter(source_path)

    assert _render_filtered_log(filter_instance, "count=%d ratio=%.3f", 42, 1.23456) == (
        "count=42 ratio=1.235"
    )


def test_obfuscate_vin_tokens_masks_bare_vin_contexts() -> None:
    text = (
        "Auto-schedule status: {'LRWYHCFS3PC901374': False}; "
        "Multi-vehicle decision for Keksla (LRWYHCFS3PC901374); "
        "EV Coordinator: Vehicle LRWYHCFS3PC901374"
    )

    result = obfuscate_vin_tokens(text, _mask)

    assert VIN not in result
    assert result.count(MASKED_VIN) == 3


def test_obfuscate_vin_tokens_leaves_already_masked_values() -> None:
    text = "ChargingScheduleView: VIN LRWY*********1374"

    assert obfuscate_vin_tokens(text, _mask) == text


def test_obfuscate_vin_tokens_ignores_non_vin_tokens() -> None:
    text = "site 12345678901234567 and token ABCDEFGHJKLMNPRST"

    assert obfuscate_vin_tokens(text, _mask) == text


def test_obfuscate_log_arg_preserves_non_string_types() -> None:
    assert obfuscate_log_arg(4.939078848884e-05, _mask) == 4.939078848884e-05
    assert obfuscate_log_arg(42, _mask) == 42
    assert obfuscate_log_arg(True, _mask) is True


def test_obfuscate_log_arg_masks_vins_in_mapping_keys_before_log_formatting() -> None:
    """A %-style dictionary argument must not bypass the log filter."""
    value = {VIN: False, "nested": [VIN]}

    assert obfuscate_log_arg(value, lambda text: obfuscate_vin_tokens(text, _mask)) == {
        MASKED_VIN: False,
        "nested": [MASKED_VIN],
    }


def test_child_vin_filter_preserves_format_types_and_is_reload_idempotent():
    logger = logging.Logger('powersync.test.child', level=logging.DEBUG)
    stream = io.StringIO()
    logger.addHandler(logging.StreamHandler(stream))
    _MODULE.install_vin_log_filter(logger)
    _MODULE.install_vin_log_filter(logger)
    assert len(logger.filters) == 1
    logger.info('vehicle %s at %.2f kW: %s', VIN, 7.0, {VIN: True})
    logger.debug(f'discovered {VIN}')
    text = stream.getvalue()
    assert VIN not in text
    assert MASKED_VIN in text
    assert '7.00 kW' in text


def test_expo_push_logging_never_contains_registered_token() -> None:
    """A diagnostic log must not disclose the credential used to send pushes."""
    actions_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "power_sync"
        / "automations"
        / "actions.py"
    )
    source = actions_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_send_expo_push"
    )
    segment = ast.get_source_segment(source, function)
    assert segment is not None

    package_name = "powersync_push_logging_testpkg"
    package = types.ModuleType(package_name)
    package.__path__ = []
    automations = types.ModuleType(f"{package_name}.automations")
    automations.__path__ = []
    const = types.ModuleType(f"{package_name}.const")
    const.DOMAIN = "power_sync"
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.automations"] = automations
    sys.modules[f"{package_name}.const"] = const

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"data":[{"status":"ok","id":"receipt"}]}'

        async def json(self):
            return {"data": [{"status": "ok", "id": "receipt"}]}

    class ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return Response()

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientSession = ClientSession
    original_aiohttp = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = aiohttp

    stream = io.StringIO()
    logger = logging.getLogger("powersync.push_logging.regression")
    handler = logging.StreamHandler(stream)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    namespace = {
        "__name__": f"{package_name}.automations.actions",
        "__package__": f"{package_name}.automations",
        "HomeAssistant": object,
        "_LOGGER": logger,
    }
    exec(textwrap.dedent(segment), namespace)

    token = "ExponentPushToken[secret-ticket-345-token]"
    hass = types.SimpleNamespace(
        data={
            "power_sync": {
                "push_tokens": {
                    token: {
                        "token": token,
                        "platform": "android",
                        "device_name": "phone",
                        "registered_at": "2026-08-02T00:00:00Z",
                    }
                }
            }
        }
    )
    try:
        asyncio.run(namespace["_send_expo_push"](hass, "Title", "Body"))
    finally:
        if original_aiohttp is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = original_aiohttp
        for name in (
            f"{package_name}.const",
            f"{package_name}.automations",
            package_name,
        ):
            sys.modules.pop(name, None)

    assert token not in stream.getvalue()
