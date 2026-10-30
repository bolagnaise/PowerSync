"""Administrator view integration tests without importing the HA runtime."""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web
import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/power_sync"
TREE = ast.parse((COMPONENT / "__init__.py").read_text())
SELECTED = {"OptimizationChatGPTView", "OptimizationSettingsView", "_optimization_chatgpt_auth", "_optimization_ai_settings",
            "_optimization_ai_service", "_optimization_ai_current_options", "_async_options_update_listener"}
VIEW_AST = ast.Module(body=[node for node in TREE.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name in SELECTED], type_ignores=[])

# Exercise the actual setup block, including its provider gate, without loading
# the hardware coordinators in the rest of async_setup_entry.
SETUP = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
SETUP_START = next(index + 1 for index, node in enumerate(SETUP.body)
                   if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and any(isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                           and arg.func.id == "OptimizationChatGPTView" for arg in node.value.args))
SETUP_END = next(index for index in range(SETUP_START, len(SETUP.body))
                 if isinstance(SETUP.body[index], ast.Expr) and isinstance(SETUP.body[index].value, ast.Call)
                 and any(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                         and arg.value.startswith("Optimization HTTP endpoints registered")
                         for arg in SETUP.body[index].value.args))
SETUP_AUTH = ast.parse("async def setup_chatgpt_auth(hass, entry): pass").body[0]
SETUP_AUTH.body = SETUP.body[SETUP_START:SETUP_END]
SETUP_AUTH_AST = ast.fix_missing_locations(ast.Module(body=[SETUP_AUTH], type_ignores=[]))


class Request(dict):
    def __init__(self, payload=None, user=None, invalid_json=False):
        super().__init__(hass_user=user)
        self.payload = payload
        self.invalid_json = invalid_json
        self.body_reads = 0

    async def json(self):
        self.body_reads += 1
        if self.invalid_json:
            raise ValueError("SECRET-invalid-body")
        return self.payload


class Manager:
    def __init__(self, auth_error):
        self.error = auth_error
        self.connected = False
        self.calls = []
        self.catalog = [{"id": "gpt-6.1-sol", "name": "GPT"}, {"id": "gpt-extra", "name": "Extra"}]
        self.catalog_failure = False
        self.logout_warning = False
        self.access_token = "SECRET-access"
        self.refresh_token = "SECRET-refresh"

    def status(self):
        return {"connected": self.connected, "status": "connected" if self.connected else "disconnected",
                "account": {"email": "user@example.test"} if self.connected else None,
                "scopes": [], "pending": None}

    def login_restart_required(self, login_id, owner): return True

    async def async_load(self): self.calls.append(("load",))
    async def async_start_login(self, owner):
        self.calls.append(("start", owner))
        return {"login_id": "login-one", "auth_url": "https://auth.openai.com/authorize?state=opaque", "expires_at": 2000}
    async def async_complete_login(self, url, login_id, owner):
        self.calls.append(("complete", url, login_id, owner))
        self.connected = True
        return self.status()
    async def async_cancel_login(self, login_id, owner):
        self.calls.append(("cancel", login_id, owner))
    async def async_models(self):
        self.calls.append(("models",))
        if self.catalog_failure:
            raise self.error("network_error", "ChatGPT is temporarily unreachable.", 503)
        return self.catalog
    async def async_logout(self):
        self.calls.append(("logout",))
        self.connected = False
        result = self.status()
        if self.logout_warning:
            result["warning"] = "Remote disconnection could not be confirmed."
        return result


class Service:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.invalidations = 0
        self.scheduled = []
    def invalidate(self): self.invalidations += 1
    def schedule_auto_refresh(self, **kwargs): self.scheduled.append(kwargs)


@pytest.fixture
def fixture(monkeypatch):
    prefix = "_powersync_chatgpt_view_test"
    for name in (prefix, prefix + ".power_sync", prefix + ".power_sync.optimization"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    modules = {}
    for name, file in (("registry_compat", "registry_compat.py"), ("const", "const.py"), ("optimization.ai_summary", "optimization/ai_summary.py"),
                       ("optimization.chatgpt_auth", "optimization/chatgpt_auth.py")):
        full_name = prefix + ".power_sync." + name
        spec = importlib.util.spec_from_file_location(full_name, COMPONENT / file)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, full_name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    ai, auth, const = modules["optimization.ai_summary"], modules["optimization.chatgpt_auth"], modules["const"]
    manager = Manager(auth.ChatGPTAuthError)
    service = Service()
    entry = SimpleNamespace(entry_id="entry-one", data={}, options={const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER: "gemini",
                                                                       const.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY: "SECRET-api-key"})
    entry_data = {"_optimization_ai_summary_service": service}
    updates, reloads, factory_calls = [], [], []
    class Entries:
        def async_entries(self, domain): return [entry] if self.configured else []
        def async_get_entry(self, entry_id): return entry if entry_id == entry.entry_id else None
        def async_update_entry(self, updated_entry, *, options):
            updates.append(copy.deepcopy(options))
            updated_entry.options = options
        async def async_reload(self, entry_id): reloads.append(entry_id)
        configured = True
    hass = SimpleNamespace(data={"power_sync": {entry.entry_id: entry_data}}, config_entries=Entries())
    def factory(actual_hass, entry_id):
        factory_calls.append((actual_hass, entry_id))
        return manager
    monkeypatch.setattr(auth, "get_chatgpt_auth", factory)
    namespace = {"__name__": prefix + ".power_sync.extracted", "__package__": prefix + ".power_sync",
                 "HomeAssistant": Any, "ConfigEntry": Any, "HomeAssistantView": object, "Any": Any, "web": web,
                 "DOMAIN": "power_sync", "_LOGGER": logging.getLogger("powersync-chatgpt-view-test"),
                 "async_get_clientsession": lambda hass: "test-session"}
    exec(compile(VIEW_AST, str(COMPONENT / "__init__.py"), "exec"), namespace)
    exec(compile(SETUP_AUTH_AST, str(COMPONENT / "__init__.py"), "exec"), namespace)
    view = namespace["OptimizationChatGPTView"](hass)
    admin = SimpleNamespace(id="admin-one", is_admin=True)
    return SimpleNamespace(view=view, hass=hass, entry=entry, entry_data=entry_data, manager=manager, service=service,
                           ai=ai, auth=auth, const=const, updates=updates, reloads=reloads, admin=admin,
                           namespace=namespace, factory_calls=factory_calls, monkeypatch=monkeypatch)


def body(response): return json.loads(response.text)
def post(fixture, payload, **kwargs): return asyncio.run(fixture.view.post(Request(payload, fixture.admin, **kwargs)))


@pytest.mark.parametrize("provider", [None, "gemini", "openai", "openrouter", "local_openai_compatible"])
def test_startup_does_not_load_unused_chatgpt_storage(fixture, provider, tmp_path, caplog):
    """Old startup may have left public registration metadata, without a login."""
    path = tmp_path / "chatgpt-auth"
    record = {"host_id": "urn:uuid:00000000-0000-0000-0000-000000000001"}
    path.write_text(json.dumps({"data": record}))
    path.chmod(0o644)
    original = path.read_bytes()
    calls = []

    class DiskStore:
        async def async_load(self):
            calls.append("load")
            return copy.deepcopy(record)

        async def async_save(self, data):
            pytest.fail("Unused ChatGPT storage must not be written")

    async def executor(function, *args):
        return function(*args)

    fixture.hass.async_add_executor_job = executor
    store = DiskStore()
    store.path = path
    manager = fixture.auth.ChatGPTAuthManager(None, fixture.auth._PrivateCredentialStore(fixture.hass, store))
    fixture.monkeypatch.setattr(fixture.auth, "get_chatgpt_auth", lambda *_: manager)
    fixture.entry.data.clear()
    fixture.entry.options.clear()
    if provider is not None:
        # Explicit options must override a previously selected ChatGPT provider.
        fixture.entry.data[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"
        fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = provider
    with caplog.at_level(logging.WARNING):
        asyncio.run(fixture.namespace["setup_chatgpt_auth"](fixture.hass, fixture.entry))
    assert calls == []
    assert "ChatGPT connection unavailable during setup" not in caplog.text
    assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("source", ["data", "options"])
def test_startup_restores_selected_chatgpt_connection(fixture, source):
    fixture.entry.options.clear()
    # Legacy optional model validation must not prevent integration startup.
    fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL] = "invalid model?"
    getattr(fixture.entry, source)[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"
    asyncio.run(fixture.namespace["setup_chatgpt_auth"](fixture.hass, fixture.entry))
    assert fixture.manager.calls == [("load",)]


def test_startup_reports_selected_chatgpt_storage_failure_without_aborting(fixture, caplog):
    fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"

    async def fail_load():
        raise fixture.auth.ChatGPTAuthError("storage_unavailable", "Storage unavailable", 503)

    fixture.manager.async_load = fail_load
    with caplog.at_level(logging.WARNING):
        asyncio.run(fixture.namespace["setup_chatgpt_auth"](fixture.hass, fixture.entry))
    assert "ChatGPT connection unavailable during setup: storage_unavailable" in caplog.text


@pytest.mark.parametrize("endpoint", ["chatgpt", "optimization_settings"])
def test_selecting_chatgpt_restores_saved_account_without_a_prior_status_request(fixture, endpoint):
    from test_chatgpt_auth import Session, Store, connect
    import time

    async def run():
        clock, store, session = [time.time()], Store(), Session()
        original = fixture.auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(original, session, clock)
        saved = copy.deepcopy(store.record)
        manager = fixture.auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        fixture.monkeypatch.setattr(fixture.auth, "get_chatgpt_auth", lambda *_: manager)
        await fixture.namespace["setup_chatgpt_auth"](fixture.hass, fixture.entry)
        assert not manager.status()["connected"]
        network_calls = len(session.calls)
        view = (fixture.view if endpoint == "chatgpt" else
                fixture.namespace["OptimizationSettingsView"](fixture.hass))
        payload = {"ai_summary_provider": "chatgpt"}
        if endpoint == "chatgpt":
            payload["action"] = "settings"
        response = await view.post(Request(payload, fixture.admin))
        assert response.status == 200
        settings = body(response).get("settings", body(response))
        assert settings["ai_summary_key_configured"]
        assert manager.status()["connected"]
        assert store.record == saved and len(session.calls) == network_calls

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["chatgpt", "optimization_settings"])
def test_selecting_chatgpt_preserves_storage_errors(fixture, endpoint):
    async def fail_load():
        raise fixture.auth.ChatGPTAuthError("storage_unavailable", "Storage unavailable", 503)

    fixture.manager.async_load = fail_load
    view = (fixture.view if endpoint == "chatgpt" else
            fixture.namespace["OptimizationSettingsView"](fixture.hass))
    payload = {"ai_summary_provider": "chatgpt"}
    if endpoint == "chatgpt":
        payload["action"] = "settings"
    response = asyncio.run(view.post(Request(payload, fixture.admin)))
    assert response.status == 503 and body(response)["code"] == "storage_unavailable"
    assert "SECRET" not in response.text


@pytest.fixture
def real_auth(fixture):
    # Reuse only provider/storage boundary doubles and signed identity fixtures.
    from test_chatgpt_auth import Session, Store, callback, token

    clock = [1_800_000_000.0]
    session, store = Session(), Store()
    manager = fixture.auth.ChatGPTAuthManager(session, store, lambda: clock[0])
    fixture.monkeypatch.setattr(fixture.auth, "get_chatgpt_auth", lambda *_: manager)
    fixture.manager = manager
    return SimpleNamespace(manager=manager, session=session, store=store, clock=clock, callback=callback, token=token)


def test_real_api_consumed_code_requires_restart_but_bad_callback_can_retry(fixture, real_auth):
    async def run():
        async def send(payload):
            return await fixture.view.post(Request(payload, fixture.admin))

        start = body(await send({"action": "start"}))
        wrong_callback = real_auth.callback(start, state="wrong-state")
        response = await send({"action": "complete", "login_id": start["login_id"], "callback_url": wrong_callback})
        assert body(response)["code"] == "invalid_callback"
        assert body(response)["login_restart_required"] is False
        status = body(await fixture.view.get(Request(user=fixture.admin)))
        assert status["pending"]["login_id"] == start["login_id"]
        real_auth.session.tokens.append(aiohttp.ClientConnectionError("SECRET-provider-details"))
        complete = {"action": "complete", "login_id": start["login_id"], "callback_url": real_auth.callback(start)}
        failed = await send(complete)
        assert failed.status == 503 and body(failed)["code"] == "network_error"
        assert body(failed)["login_restart_required"] is True
        for payload in (complete, {"action": "cancel", "login_id": start["login_id"]}):
            expired = await send(payload)
            assert body(expired)["code"] == "login_expired"
            assert body(expired)["login_restart_required"] is True
        for error in (response, failed, expired):
            assert "SECRET" not in error.text and "wrong-state" not in error.text
            assert fixture.admin.id not in error.text and "callback_url" not in error.text
        fresh = body(await send({"action": "start"}))
        assert fresh["login_id"] != start["login_id"]
        nonce = parse_qs(urlsplit(fresh["auth_url"]).query)["nonce"][0]
        real_auth.session.tokens.append(real_auth.token(real_auth.clock[0], nonce))
        connected = await send({"action": "complete", "login_id": fresh["login_id"], "callback_url": real_auth.callback(fresh)})
        assert connected.status == 200 and body(connected)["connected"]
        assert body(connected)["settings"]["ai_summary_provider"] == "chatgpt"
    asyncio.run(run())


@pytest.mark.parametrize("action", ["complete", "cancel"])
def test_real_api_restart_evidence_is_owner_bound_and_expiry_requires_restart(fixture, real_auth, action):
    async def run():
        async def send(payload, user=fixture.admin):
            return await fixture.view.post(Request(payload, user))

        def submission(start, login_id=None):
            payload = {"action": action, "login_id": login_id or start["login_id"]}
            if action == "complete":
                payload["callback_url"] = real_auth.callback(start)
            return payload

        start = body(await send({"action": "start"}))
        other_admin = SimpleNamespace(id="other-admin", is_admin=True)
        for payload, user in ((submission(start), other_admin), (submission(start, "other-login"), fixture.admin)):
            denied = await send(payload, user)
            assert denied.status == 403 and body(denied)["code"] == "login_mismatch"
            assert body(denied)["login_restart_required"] is True
            assert "SECRET" not in denied.text and "other-admin" not in denied.text
        non_admin = SimpleNamespace(id="ordinary-user", is_admin=False)
        denied = await send(submission(start), non_admin)
        assert denied.status == 403 and body(denied)["code"] == "admin_required"
        assert "login_restart_required" not in body(denied)
        corrected_input = await send({"action": "complete", "login_id": start["login_id"], "callback_url": []})
        assert body(corrected_input)["code"] == "invalid_callback"
        assert body(corrected_input)["login_restart_required"] is False
        status = body(await fixture.view.get(Request(user=fixture.admin)))
        assert status["pending"]["login_id"] == start["login_id"]
        nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
        real_auth.session.tokens.append(real_auth.token(real_auth.clock[0], nonce))
        connected = await send({"action": "complete", "login_id": start["login_id"], "callback_url": real_auth.callback(start)})
        assert connected.status == 200 and body(connected)["connected"]

        expiring = body(await send({"action": "start"}))
        real_auth.clock[0] += 601
        expired = await send(submission(expiring))
        assert expired.status == 410 and body(expired)["code"] == "login_expired"
        assert body(expired)["login_restart_required"] is True
        fresh = body(await send({"action": "start"}))
        assert fresh["login_id"] != expiring["login_id"]
        cancelled = await send({"action": "cancel", "login_id": fresh["login_id"]})
        assert cancelled.status == 200 and body(cancelled)["pending"] is None
        assert body(cancelled)["connected"]
    asyncio.run(run())


def test_real_api_invalidated_account_requires_explicit_reset_for_other_identity(fixture, real_auth):
    from test_chatgpt_auth import Response

    async def run():
        async def send(payload, user=fixture.admin):
            return await fixture.view.post(Request(payload, user))

        async def sign_in(subject, email, access_token="SECRET-access"):
            start = body(await send({"action": "start"}))
            nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
            real_auth.session.tokens.append(real_auth.token(real_auth.clock[0], nonce, claims={"sub": subject, "email": email},
                                                            access_token=access_token))
            return await send({"action": "complete", "login_id": start["login_id"], "callback_url": real_auth.callback(start)})

        connected = await sign_in("subject-one", "old@example.test")
        assert connected.status == 200 and body(connected)["connected"]
        real_auth.clock[0] += 3600
        real_auth.session.tokens.append(Response({"error": "invalid_grant", "error_description": "SECRET-provider-details"}, 400))
        expired_grant = await send({"action": "models"})
        assert expired_grant.status == 401 and body(expired_grant)["code"] == "invalid_grant"
        status = body(await fixture.view.get(Request(user=fixture.admin)))
        assert not status["connected"] and status["status"] == "reconnect_required"
        denied_reset = await send({"action": "logout"}, SimpleNamespace(id="ordinary-user", is_admin=False))
        assert denied_reset.status == 403
        mismatch = await sign_in("subject-two", "new@example.test")
        assert mismatch.status == 400 and body(mismatch)["code"] == "account_mismatch"
        assert body(mismatch)["login_restart_required"] is True
        reset = await send({"action": "logout"})
        assert reset.status == 200 and body(reset)["status"] == "disconnected"
        replacement = await sign_in("subject-two", "new@example.test", "SECRET-replacement-access")
        assert replacement.status == 200 and body(replacement)["connected"]
        assert body(replacement)["account"]["email"] == "new@example.test"
        assert await real_auth.manager.async_access_token() == "SECRET-replacement-access"
        for response in (expired_grant, denied_reset, mismatch, reset, replacement):
            assert "SECRET" not in response.text and "subject-two" not in response.text
    asyncio.run(run())


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("user", [None, SimpleNamespace(id="ordinary-user", is_admin=False)])
def test_admin_gate_precedes_body_factory_and_provider_access(fixture, method, user):
    request = Request({"action": "start", "SECRET": "SECRET"}, user=user)
    response = asyncio.run(getattr(fixture.view, method)(request))
    assert response.status == 403
    assert body(response)["code"] == "admin_required"
    assert request.body_reads == 0 and not fixture.factory_calls and not fixture.manager.calls
    assert "SECRET" not in response.text


def test_unconfigured_entry_rejected_before_auth_factory(fixture):
    fixture.hass.config_entries.configured = False
    request = Request({"action": "start"}, fixture.admin)
    response = asyncio.run(fixture.view.post(request))
    assert response.status == 409 and body(response)["code"] == "integration_not_configured"
    assert not fixture.factory_calls and not fixture.manager.calls and request.body_reads == 0


@pytest.mark.parametrize("payload", [None, [], "SECRET", {}, {"action": None}, {"action": []}, {"action": "unknown"},
                                    {"action": "start", "callback_url": "SECRET"}, {"action": "logout", "api_key": "SECRET"},
                                    {"action": "models", "entry_id": "other"}, {"action": "settings"},
                                    {"action": "settings", "unsupported_model": "SECRET"}])
def test_strict_actions_and_allowed_fields(fixture, payload):
    response = post(fixture, payload)
    assert response.status == 400 and body(response)["code"] == "invalid_request"
    assert not fixture.manager.calls and not fixture.updates
    assert "SECRET" not in response.text


def test_invalid_json_sanitized(fixture):
    response = post(fixture, None, invalid_json=True)
    assert response.status == 400 and "SECRET" not in response.text


@pytest.mark.parametrize("payload,code", [({"action": "complete", "login_id": "", "callback_url": "url"}, "invalid_request"),
                                         ({"action": "cancel", "login_id": "x" * 129}, "invalid_request"),
                                         ({"action": "cancel", "login_id": 123}, "invalid_request"),
                                         ({"action": "complete", "login_id": "login", "callback_url": ""}, "invalid_callback"),
                                         ({"action": "complete", "login_id": "login", "callback_url": "x" * 16385}, "invalid_callback"),
                                         ({"action": "complete", "login_id": "login", "callback_url": []}, "invalid_callback")])
def test_callback_and_login_bounds_before_provider(fixture, payload, code):
    response = post(fixture, payload)
    assert response.status == 400 and body(response)["code"] == code
    assert not fixture.manager.calls


def test_start_binds_administrator_and_returns_only_pending_metadata(fixture):
    response = post(fixture, {"action": "start"})
    assert response.status == 200 and fixture.manager.calls == [("start", "admin-one")]
    assert set(body(response)) == {"success", "login_id", "auth_url", "expires_at"}
    assert not fixture.updates and "SECRET" not in response.text


def test_status_loads_credentials_but_returns_public_readiness(fixture):
    fixture.manager.connected = True
    fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"
    response = asyncio.run(fixture.view.get(Request(user=fixture.admin)))
    result = body(response)
    assert fixture.manager.calls == [("load",)]
    assert result["connected"] and result["settings"]["ai_summary_key_configured"]
    assert "SECRET" not in response.text and "api_key" not in result


def test_complete_selects_chatgpt_catalog_model_and_invalidates(fixture):
    fixture.manager.catalog = [{"id": "gpt-extra", "name": "Extra"}]
    callback = "http://127.0.0.1:1455/auth/callback?code=SECRET-callback"
    response = post(fixture, {"action": "complete", "login_id": "login-one", "callback_url": callback})
    result = body(response)
    assert fixture.manager.calls == [("complete", callback, "login-one", "admin-one"), ("models",)]
    assert result["connected"] and result["settings"]["ai_summary_provider"] == "chatgpt"
    assert result["settings"]["ai_summary_openai_model"] == "gpt-extra"
    assert result["models"] == fixture.manager.catalog
    assert fixture.ai.configured_api_key(fixture.entry) == ""
    assert fixture.entry_data["_skip_reload"] and fixture.service.invalidations == 1
    assert "SECRET" not in response.text


def test_complete_preserves_available_selected_model(fixture):
    fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL] = "gpt-extra"
    response = post(fixture, {"action": "complete", "login_id": "login-one", "callback_url": "callback"})
    assert response.status == 200
    assert body(response)["settings"]["ai_summary_openai_model"] == "gpt-extra"


def test_catalog_outage_after_complete_preserves_connected_selected_account(fixture, caplog):
    fixture.manager.catalog_failure = True
    response = post(fixture, {"action": "complete", "login_id": "login-one", "callback_url": "SECRET-callback"})
    result = body(response)
    assert response.status == 200 and result["connected"]
    assert result["settings"]["ai_summary_provider"] == "chatgpt"
    assert result["settings"]["ai_summary_key_configured"] and result["models"] == []
    assert fixture.service.invalidations == 1
    assert "SECRET" not in response.text and "SECRET" not in caplog.text


def test_cancel_binds_administrator_without_settings_update(fixture):
    response = post(fixture, {"action": "cancel", "login_id": "login-one"})
    assert response.status == 200 and fixture.manager.calls == [("cancel", "login-one", "admin-one")]
    assert not fixture.updates and fixture.service.invalidations == 0


def test_logout_invalidates_and_readiness_tracks_connection(fixture):
    fixture.manager.connected = True
    fixture.manager.logout_warning = True
    fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"
    response = post(fixture, {"action": "logout"})
    result = body(response)
    assert response.status == 200 and not result["connected"]
    assert not result["settings"]["ai_summary_key_configured"]
    assert "warning" in result and fixture.manager.calls == [("logout",)]
    assert fixture.service.invalidations == 1 and not fixture.updates
    assert "SECRET" not in response.text


def test_settings_are_write_only_invalidate_and_skip_reload(fixture):
    response = post(fixture, {"action": "settings", "ai_summary_provider": "openai", "ai_summary_openai_model": "gpt-extra",
                              "ai_summary_api_key": "SECRET-replacement"})
    result = body(response)
    assert response.status == 200 and result["settings"]["ai_summary_api_key_configured"]
    assert result["settings"]["ai_summary_provider"] == "openai" and result["settings"]["ai_summary_openai_model"] == "gpt-extra"
    assert fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY] == "SECRET-replacement"
    assert fixture.entry_data["_skip_reload"] and fixture.service.invalidations == 1
    assert not fixture.manager.calls and "SECRET" not in response.text
    asyncio.run(fixture.namespace["_async_options_update_listener"](fixture.hass, fixture.entry))
    assert "_skip_reload" not in fixture.entry_data and not fixture.reloads
    asyncio.run(fixture.namespace["_async_options_update_listener"](fixture.hass, fixture.entry))
    assert fixture.reloads == ["entry-one"]


@pytest.mark.parametrize("fields,code", [({"ai_summary_provider": "SECRET"}, "invalid_ai_provider"),
                                       ({"ai_summary_openai_model": "bad model"}, "invalid_openai_model"),
                                       ({"ai_summary_api_key": []}, "invalid_ai_api_key"),
                                       ({"clear_ai_summary_api_key": "true"}, "invalid_ai_settings"),
                                       ({"clear_ai_summary_api_key": True, "ai_summary_api_key": "SECRET"}, "invalid_ai_settings")])
def test_invalid_settings_atomically_preserve_options(fixture, fields, code):
    before = copy.deepcopy(fixture.entry.options)
    response = post(fixture, {"action": "settings", **fields})
    assert response.status == 400 and body(response)["code"] == code
    assert fixture.entry.options == before and not fixture.updates
    assert not fixture.entry_data.get("_skip_reload") and fixture.service.invalidations == 0
    assert "SECRET" not in response.text


def test_models_failure_has_safe_error_without_settings_changes(fixture):
    fixture.manager.catalog_failure = True
    response = post(fixture, {"action": "models"})
    assert response.status == 503 and body(response)["code"] == "network_error"
    assert not fixture.updates and "SECRET" not in response.text


def test_helper_service_factory_injects_same_auth_and_selected_model(fixture):
    fixture.entry_data.pop("_optimization_ai_summary_service")
    fixture.monkeypatch.setattr(fixture.ai, "AISummaryService", Service)
    fixture.entry.options.update({fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER: "chatgpt",
                                  fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL: "gpt-extra",
                                  fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_AUTO_REFRESH_INTERVAL: 30,
                                  fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_AUTO_REFRESH: True})
    fixture.manager.connected = True
    callbacks = []
    coordinator = SimpleNamespace(get_api_data=lambda: {}, async_add_listener=lambda fn: callbacks.append(fn) or "unsub")
    factory = fixture.namespace["_optimization_ai_service"]
    service = factory(fixture.hass, fixture.entry.entry_id, coordinator)
    assert service.kwargs["chatgpt_auth"] is fixture.manager
    assert service.args[0] == "test-session" and fixture.entry_data["_optimization_ai_summary_auto_unsub"] == "unsub"
    callbacks[0]()
    assert service.scheduled[0]["provider"] == "chatgpt" and service.scheduled[0]["openai_model"] == "gpt-extra"
    assert service.scheduled[0]["auto_refresh_interval_minutes"] == 30
    assert factory(fixture.hass, fixture.entry.entry_id, coordinator) is service and len(callbacks) == 1
    assert factory(fixture.hass, "missing-entry", coordinator) is None


@pytest.mark.parametrize("fields", [{"clear_ai_summary_api_key": True}, {"ai_summary_provider": "openai"}])
def test_clear_or_switch_never_reactivates_inherited_provider_key(fixture, fields):
    key = fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY
    fixture.entry.options.pop(key)
    fixture.entry.data[key] = "SECRET-inherited-provider-key"
    response = post(fixture, {"action": "settings", **fields})
    assert response.status == 200
    assert not body(response)["settings"]["ai_summary_api_key_configured"]
    assert fixture.ai.configured_api_key(fixture.entry) == ""
    assert "SECRET" not in response.text


def test_unchanged_settings_invalidate_without_suppressing_next_real_reload(fixture):
    response = post(fixture, {"action": "settings", "ai_summary_provider": "gemini"})
    assert response.status == 200 and fixture.service.invalidations == 1
    assert not fixture.updates and not fixture.entry_data.get("_skip_reload")


@pytest.mark.parametrize("endpoint", ["chatgpt", "optimization_settings"])
def test_interval_only_settings_are_saved_and_invalidate_queued_auto_work(fixture, endpoint):
    view = fixture.view if endpoint == "chatgpt" else fixture.namespace["OptimizationSettingsView"](fixture.hass)
    payload = {"ai_summary_auto_refresh_interval": 30}
    if endpoint == "chatgpt":
        payload["action"] = "settings"
    response = asyncio.run(view.post(Request(payload, fixture.admin)))
    assert response.status == 200
    assert fixture.entry.options[fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_AUTO_REFRESH_INTERVAL] == 30
    assert fixture.service.invalidations == 1


def test_missing_coordinator_does_not_create_summary_service(fixture):
    fixture.entry_data.pop("_optimization_ai_summary_service")
    assert fixture.namespace["_optimization_ai_service"](fixture.hass, fixture.entry.entry_id, None) is None
    assert not fixture.factory_calls


@pytest.mark.parametrize("method", ["get", "post"])
def test_unloaded_entry_rejected_before_body_or_manager(fixture, method):
    fixture.hass.data["power_sync"].clear()
    request = Request({"action": "start"}, fixture.admin)
    response = asyncio.run(getattr(fixture.view, method)(request))
    assert response.status == 409 and body(response)["code"] == "integration_unavailable"
    assert request.body_reads == 0 and not fixture.factory_calls and not fixture.manager.calls


def test_same_inherited_provider_preserves_its_key(fixture):
    provider = fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER
    key = fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY
    fixture.entry.options.clear()
    fixture.entry.data.update({provider: "openai", key: "SECRET-openai-key"})
    response = post(fixture, {"action": "settings", "ai_summary_provider": "openai", "ai_summary_openai_model": "gpt-extra"})
    assert response.status == 200 and body(response)["settings"]["ai_summary_api_key_configured"]
    assert fixture.ai.configured_api_key(fixture.entry) == "SECRET-openai-key"
    assert "SECRET" not in response.text


@pytest.mark.parametrize("user", [None, SimpleNamespace(id="ordinary-user", is_admin=False)])
@pytest.mark.parametrize("field,value", [("ai_summary_provider", "chatgpt"), ("ai_summary_api_key", "SECRET"),
                                        ("ai_summary_openai_model", "gpt-extra"), ("ai_summary_auto_refresh", True),
                                        ("ai_summary_auto_refresh_interval", 30)])
def test_generic_settings_cannot_bypass_ai_administrator_boundary(fixture, user, field, value):
    view = fixture.namespace["OptimizationSettingsView"](fixture.hass)
    before = copy.deepcopy(fixture.entry.options)
    response = asyncio.run(view.post(Request({field: value}, user)))
    assert response.status == 403 and body(response)["code"] == "admin_required"
    assert not fixture.updates and fixture.entry.options == before
    assert not fixture.factory_calls and not fixture.manager.calls
    assert "SECRET" not in response.text


@pytest.mark.parametrize("provider, fields, expected_model", [
    ("gemini", {}, "gemini-3.5-flash-lite"),
    ("grok", {}, "grok-4.5"),
    ("openrouter", {"ai_summary_openrouter_model": "anthropic/claude-sonnet-4"}, "anthropic/claude-sonnet-4"),
    ("local_openai_compatible", {"ai_summary_local_endpoint": "http://127.0.0.1:11434/v1", "ai_summary_local_model": "llama3.2"}, "llama3.2"),
])
def test_dashboard_settings_save_each_existing_provider_without_credentials_in_response(fixture, provider, fields, expected_model):
    response = post(fixture, {"action": "settings", "ai_summary_provider": provider,
                              "ai_summary_api_key": "SECRET-new-provider-key", "ai_summary_auto_refresh": True, **fields})
    assert response.status == 200
    settings = body(response)["settings"]
    assert settings["ai_summary_provider"] == provider
    assert settings["ai_summary_model"] == expected_model
    assert settings["ai_summary_auto_refresh"] is True
    assert settings["ai_summary_key_configured"] is True
    assert fixture.ai.configured_api_key(fixture.entry) == "SECRET-new-provider-key"
    assert "ai_summary_api_key" not in settings and "SECRET" not in response.text
    assert fixture.service.invalidations == 1 and fixture.entry_data["_skip_reload"] is True
    assert fixture.manager.calls == []
    if provider == "local_openai_compatible":
        assert settings["ai_summary_local_endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
        assert settings["ai_summary_local_model"] == "llama3.2"
    if provider == "openrouter":
        assert settings["ai_summary_openrouter_model"] == expected_model


@pytest.mark.parametrize("provider, fields", [
    ("grok", {}),
    ("openrouter", {"ai_summary_openrouter_model": "anthropic/claude-sonnet-4"}),
    ("local_openai_compatible", {"ai_summary_local_endpoint": "http://127.0.0.1:11434", "ai_summary_local_model": "llama3.2"}),
])
def test_dashboard_provider_switch_clears_old_key_without_replacement(fixture, provider, fields):
    response = post(fixture, {"action": "settings", "ai_summary_provider": provider, **fields})
    assert response.status == 200
    assert fixture.ai.configured_api_key(fixture.entry) == ""
    settings = body(response)["settings"]
    assert settings["ai_summary_api_key_configured"] is False
    assert settings["ai_summary_key_configured"] is (provider == "local_openai_compatible")
    assert "SECRET" not in response.text


def test_dashboard_same_provider_blank_key_and_form_get_preserve_saved_values(fixture):
    fixture.entry.options.update({fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER: "openrouter",
                                  fixture.const.CONF_OPTIMIZATION_AI_SUMMARY_OPENROUTER_MODEL: "anthropic/claude-sonnet-4"})
    response = post(fixture, {"action": "settings", "ai_summary_provider": "openrouter",
                              "ai_summary_api_key": " ", "ai_summary_auto_refresh": True})
    assert response.status == 200
    assert fixture.ai.configured_api_key(fixture.entry) == "SECRET-api-key"
    loaded = asyncio.run(fixture.view.get(Request(user=fixture.admin)))
    assert loaded.status == 200
    settings = body(loaded)["settings"]
    assert settings["ai_summary_openrouter_model"] == "anthropic/claude-sonnet-4"
    assert settings["ai_summary_auto_refresh"] is True and settings["ai_summary_api_key_configured"] is True
    assert "SECRET" not in loaded.text and "ai_summary_api_key" not in settings


@pytest.mark.parametrize("fields, code", [
    ({"ai_summary_local_endpoint": []}, "invalid_local_ai_endpoint"),
    ({"ai_summary_local_model": 123}, "invalid_local_ai_model"),
    ({"ai_summary_local_model": "x" * 129}, "invalid_local_ai_model"),
    ({"ai_summary_openrouter_model": []}, "invalid_openrouter_model"),
    ({"ai_summary_openrouter_model": "x" * 129}, "invalid_openrouter_model"),
    ({"ai_summary_auto_refresh": "true"}, "invalid_ai_settings"),
    ({"ai_summary_provider": "openrouter"}, "invalid_openrouter_model"),
    ({"ai_summary_provider": "local_openai_compatible", "ai_summary_local_model": "llama3.2"}, "invalid_local_ai_endpoint"),
    ({"ai_summary_provider": "local_openai_compatible", "ai_summary_local_endpoint": "http://SECRET@127.0.0.1", "ai_summary_local_model": "llama3.2"}, "invalid_local_ai_endpoint"),
    ({"ai_summary_provider": "local_openai_compatible", "ai_summary_local_endpoint": "http://169.254.169.254", "ai_summary_local_model": "llama3.2"}, "unsafe_local_ai_endpoint"),
])
def test_dashboard_extended_settings_reject_invalid_fields_atomically(fixture, fields, code):
    before = copy.deepcopy(fixture.entry.options)
    response = post(fixture, {"action": "settings", **fields})
    assert response.status == 400 and body(response)["code"] == code
    assert fixture.entry.options == before and not fixture.updates
    assert fixture.service.invalidations == 0 and fixture.manager.calls == []
    assert "SECRET" not in response.text


@pytest.mark.parametrize("user", [None, SimpleNamespace(id="ordinary-user", is_admin=False)])
def test_dashboard_extended_settings_are_admin_only(fixture, user):
    request = Request({"action": "settings", "ai_summary_provider": "local_openai_compatible",
                       "ai_summary_local_endpoint": "http://127.0.0.1:11434", "ai_summary_local_model": "llama3.2",
                       "ai_summary_auto_refresh": True}, user)
    response = asyncio.run(fixture.view.post(request))
    assert response.status == 403 and request.body_reads == 0
    assert not fixture.updates and not fixture.manager.calls


def lifecycle_namespace(fixture):
    names = {"async_unload_entry", "async_reload_entry", "async_remove_entry"}
    tree = ast.Module(body=[node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                           and node.name in names], type_ignores=[])
    namespace = dict(fixture.namespace, asyncio=asyncio, PLATFORMS=[],
                     SERVICE_SYNC_TOU="sync_tou", SERVICE_SYNC_NOW="sync_now")
    exec(compile(tree, str(COMPONENT / "__init__.py"), "exec"), namespace)
    return namespace


def test_auth_shutdown_failure_still_cleans_hardware_and_allows_ha_reload(fixture, caplog):
    calls = []
    class Auth:
        async def async_shutdown(self):
            calls.append("auth")
            raise OSError("SECRET-refresh-token")
    class Tesla:
        async def async_shutdown(self): calls.append("tesla")
    class Trigger:
        async def async_shutdown(self): calls.append("aemo")
    class Optimizer:
        async def disable(self): calls.append("optimizer")
    async def unload(*args):
        calls.append("platforms")
        return True
    async def setup(hass, entry):
        calls.append("setup")
        hass.data["power_sync"][entry.entry_id] = {}
        assert fixture.real_auth_factory(hass, entry.entry_id) is manager
        assert manager._closed is False
    manager = Auth()
    manager._closed = True
    spec = importlib.util.spec_from_file_location("retained_auth_test", COMPONENT / "optimization/chatgpt_auth.py")
    module = importlib.util.module_from_spec(spec)
    fixture.monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    fixture.real_auth_factory = module.get_chatgpt_auth
    storage = ModuleType("homeassistant.helpers.storage")
    storage.Store = lambda *args, **kwargs: pytest.fail("Must reuse retained manager")
    client = ModuleType("homeassistant.helpers.aiohttp_client")
    client.async_get_clientsession = lambda hass: None
    fixture.monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    fixture.monkeypatch.setitem(sys.modules, "homeassistant.helpers.aiohttp_client", client)
    native_rate = ModuleType("_powersync_chatgpt_view_test.power_sync.tesla_native_rate")
    async def stop_native_rate(hass, entry):
        calls.append("native-rate")
    native_rate.async_stop = stop_native_rate
    fixture.monkeypatch.setitem(sys.modules, native_rate.__name__, native_rate)
    fixture.entry_data.clear()
    fixture.entry_data.update(_optimization_chatgpt_auth=manager, tesla_coordinator=Tesla(),
                              aemo_dispatch_trigger_coordinator=Trigger(), optimization_coordinator=Optimizer())
    fixture.hass.config_entries.async_unload_platforms = unload
    fixture.hass.services = SimpleNamespace(async_remove=lambda *args: calls.append("remove-service"))
    namespace = lifecycle_namespace(fixture)
    namespace["async_setup_entry"] = setup
    async def ha_reload(entry_id):
        if await namespace["async_unload_entry"](fixture.hass, fixture.entry):
            await setup(fixture.hass, fixture.entry)
    fixture.hass.config_entries.async_reload = ha_reload
    with caplog.at_level(logging.WARNING):
        asyncio.run(fixture.hass.config_entries.async_reload(fixture.entry.entry_id))
    assert {"native-rate", "auth", "tesla", "aemo", "optimizer", "platforms", "setup"}.issubset(calls)
    assert fixture.hass.data["power_sync"][fixture.entry.entry_id]["_optimization_chatgpt_auth"] is manager
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("outcome", ["success", "warning", "error", "timeout"])
@pytest.mark.parametrize("loaded", [False, True])
def test_entry_removal_revokes_and_deletes_private_store(fixture, monkeypatch, caplog, outcome, loaded):
    calls, notifications = [], []
    class Manager:
        def __init__(self, *args): calls.append("fresh-manager")
        async def async_revoke_for_removal(self):
            calls.append("revoke")
            if outcome == "error":
                raise OSError("SECRET-provider-response")
            if outcome == "timeout":
                await asyncio.Event().wait()
            return {"warning": fixture.auth.REMOTE_DISCONNECTION_WARNING} if outcome == "warning" else {}
    class Store:
        def __init__(self, hass, version, key, **kwargs):
            assert version == 1
            assert key == "power_sync.chatgpt_auth.entry-one"
            assert kwargs == {"private": True, "atomic_writes": True}
        async def async_remove(self): calls.append("remove")
    storage = ModuleType("homeassistant.helpers.storage")
    storage.Store = Store
    components = ModuleType("homeassistant.components")
    components.persistent_notification = SimpleNamespace(async_create=lambda *args, **kwargs: notifications.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setattr(fixture.auth, "ChatGPTAuthManager", Manager)
    if loaded:
        fixture.hass.data["power_sync_chatgpt_auth_retained"] = {fixture.entry.entry_id: Manager()}
        calls.clear()
    else:
        fixture.hass.data["power_sync"].clear()
    namespace = lifecycle_namespace(fixture)
    real_timeout = asyncio.timeout
    namespace["asyncio"] = SimpleNamespace(timeout=lambda seconds: real_timeout(0.01))
    with caplog.at_level(logging.WARNING):
        asyncio.run(namespace["async_remove_entry"](fixture.hass, fixture.entry))
    assert calls == (["fresh-manager"] if not loaded else []) + ["revoke", "remove"]
    assert bool(notifications) is (outcome in {"warning", "error", "timeout"})
    if notifications:
        assert "disconnect powersync in chatgpt settings" in notifications[0][0][1].lower()
        assert "SECRET" not in str(notifications)
    assert "SECRET" not in caplog.text


def test_entry_removal_storage_failure_is_sanitized(fixture, monkeypatch, caplog):
    class Store:
        def __init__(self, *args, **kwargs): raise OSError("SECRET-store-path")
    storage = ModuleType("homeassistant.helpers.storage")
    storage.Store = Store
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    components = ModuleType("homeassistant.components")
    components.persistent_notification = SimpleNamespace(async_create=lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    namespace = lifecycle_namespace(fixture)
    with caplog.at_level(logging.WARNING):
        asyncio.run(namespace["async_remove_entry"](fixture.hass, fixture.entry))
    assert "ChatGPT credential storage could not be removed" in caplog.text
    assert "SECRET" not in caplog.text
