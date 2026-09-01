"""Lifecycle regressions for the bounded Amber WebSocket fetcher."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLIENT_PATH = ROOT / "custom_components" / "power_sync" / "websocket_client.py"


def _load_client_module(monkeypatch, connect):
    """Import the small client module without importing Home Assistant."""
    sensitive_logging = types.ModuleType("ticket_398_sensitive_logging")
    sensitive_logging.obfuscate_log_arg = lambda value, _obfuscate: value
    sensitive_logging.obfuscate_vin_tokens = lambda text, _obfuscate: text
    websockets = types.ModuleType("websockets")
    websockets.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", websockets)

    package = types.ModuleType("ticket_398_power_sync")
    package.__path__ = [str(CLIENT_PATH.parent)]
    monkeypatch.setitem(sys.modules, "ticket_398_power_sync", package)
    monkeypatch.setitem(
        sys.modules, "ticket_398_power_sync.sensitive_logging", sensitive_logging
    )

    spec = importlib.util.spec_from_file_location(
        "ticket_398_power_sync.websocket_client", CLIENT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stop_cancels_pending_receive_and_prevents_late_callback(monkeypatch):
    """#398: reload must not leave a prior client receiving or notifying."""
    receive_started = threading.Event()
    closed = threading.Event()

    class _Socket:
        async def send(self, _message):
            return None

        async def recv(self):
            receive_started.set()
            await asyncio.Event().wait()

    class _Connection:
        async def __aenter__(self):
            return _Socket()

        async def __aexit__(self, _exc_type, _exc, _tb):
            closed.set()
            return False

    module = _load_client_module(monkeypatch, lambda *_args, **_kwargs: _Connection())
    callbacks = []
    client = module.AmberWebSocketClient("token", "site", callbacks.append)

    asyncio.run(client.start())
    assert receive_started.wait(timeout=1)
    asyncio.run(client.stop())

    assert closed.is_set()
    assert client._thread is not None and not client._thread.is_alive()
    assert client._polling_task is None
    assert callbacks == []
    assert client._handle_message(
        '{"action":"price-update","data":{"prices":[]}}'
    ) is False
    assert client._cached_prices == {}


def test_websocket_timeout_is_logged_once_until_a_price_arrives(monkeypatch):
    """An optional unavailable stream must not emit one warning per interval."""
    records = []

    class _Socket:
        async def send(self, _message):
            return None

        async def recv(self):
            raise asyncio.TimeoutError

    class _Connection:
        async def __aenter__(self):
            return _Socket()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    module = _load_client_module(monkeypatch, lambda *_args, **_kwargs: _Connection())
    monkeypatch.setattr(
        module,
        "_LOGGER",
        types.SimpleNamespace(
            debug=lambda message, *args: records.append(("debug", message)),
            info=lambda message, *args: records.append(("info", message)),
            warning=lambda message, *args: records.append(("warning", message)),
            error=lambda message, *args, **kwargs: records.append(("error", message)),
        ),
    )
    client = module.AmberWebSocketClient("token", "site")
    client._running = True

    asyncio.run(client._fetch_price_once())
    asyncio.run(client._fetch_price_once())

    assert [level for level, _message in records].count("warning") == 1
    assert client._timeout_warning_logged is True


def test_stopped_client_does_not_open_its_startup_connection(monkeypatch):
    """A stop/start race must not create a late old-client connection."""
    connections = []

    class _Connection:
        async def __aenter__(self):
            return types.SimpleNamespace(send=lambda _message: None)

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    def connect(*_args, **_kwargs):
        connections.append(True)
        return _Connection()

    module = _load_client_module(monkeypatch, connect)
    client = module.AmberWebSocketClient("token", "site")
    client._running = False

    asyncio.run(client._interval_polling_loop())

    assert connections == []
