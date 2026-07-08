"""Regression tests for AEMO NEMWEB transient-failure backoff."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _load_aemo_api_module():
    spec = importlib.util.spec_from_file_location(
        "power_sync_aemo_api_test",
        COMPONENT_ROOT / "aemo_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, *, text=None, json_data=None, error=None, enter_error=None):
        self._text = text
        self._json_data = json_data
        self._error = error
        self._enter_error = enter_error

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    async def text(self):
        return self._text or ""

    async def json(self):
        return self._json_data


class _FakeSession:
    closed = False

    def __init__(self, module, *, dispatch_error=None, json_enter_error=None):
        self._module = module
        self._dispatch_error = dispatch_error
        self._json_enter_error = json_enter_error
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url == self._module.AEMOAPIClient.DISPATCH_URL:
            return _FakeResponse(error=self._dispatch_error)
        if url == self._module.AEMOAPIClient.BASE_URL:
            return _FakeResponse(
                enter_error=self._json_enter_error,
                json_data={
                    "ELEC_NEM_SUMMARY": [
                        {
                            "REGIONID": "NSW1",
                            "PRICE": "123.4",
                            "SETTLEMENTDATE": "2026/06/01 16:20:00",
                            "PRICE_STATUS": "FIRM",
                            "TOTALDEMAND": "1000",
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected URL: {url}")


def test_nemweb_403_starts_backoff_and_throttles_warnings(monkeypatch, caplog):
    module = _load_aemo_api_module()
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    session = _FakeSession(module, dispatch_error=Exception("403 Forbidden"))
    client = module.AEMOAPIClient(session)
    caplog.set_level(logging.DEBUG, logger=module._LOGGER.name)

    prices, is_new, filename = asyncio.run(client.get_current_prices_with_file())

    assert prices["NSW1"]["price"] == 123.4
    assert is_new is False
    assert filename == ""
    assert session.urls.count(module.AEMOAPIClient.DISPATCH_URL) == 1

    now[0] += 5
    asyncio.run(client.get_current_prices_with_file())

    assert session.urls.count(module.AEMOAPIClient.DISPATCH_URL) == 1
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING
        and "NEMWEB dispatch fetch failed" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_nemweb_backoff_uses_cached_dispatch_without_network(monkeypatch):
    module = _load_aemo_api_module()
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.0)

    session = _FakeSession(module)
    client = module.AEMOAPIClient(session)
    cached_prices = {
        "NSW1": {
            "price": 100.0,
            "timestamp": "2026/06/01 16:20:00",
            "status": "FIRM",
            "region_name": "New South Wales",
        }
    }
    filename = "PUBLIC_DISPATCHIS_202606011620_0000000520378945.zip"
    client._dispatch_cache = {filename: cached_prices}
    client._last_dispatch_file = filename
    client._nemweb_backoff_until = 30.0

    prices, is_new, result_file = asyncio.run(client.get_current_prices_with_file())

    assert prices is cached_prices
    assert is_new is False
    assert result_file == filename
    assert session.urls == []


def test_json_fallback_timeout_returns_none(caplog):
    module = _load_aemo_api_module()
    session = _FakeSession(
        module,
        dispatch_error=Exception("403 Forbidden"),
        json_enter_error=asyncio.TimeoutError(),
    )
    client = module.AEMOAPIClient(session)
    caplog.set_level(logging.ERROR, logger=module._LOGGER.name)

    prices, is_new, filename = asyncio.run(client.get_current_prices_with_file())

    assert prices is None
    assert is_new is False
    assert filename == ""
    assert any(
        "Error fetching AEMO prices (JSON fallback)" in record.getMessage()
        for record in caplog.records
    )
