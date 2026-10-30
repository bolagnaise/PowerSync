"""Lifecycle and trust-boundary tests for subscription sign-in (no live OAuth)."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import ssl
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import pytest

# PyJWT ships with Home Assistant core; skip cleanly in bare test envs without it.
jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa

MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components/power_sync/optimization/chatgpt_auth.py"
spec = importlib.util.spec_from_file_location("powersync_test_chatgpt_auth", MODULE_PATH)
auth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auth
spec.loader.exec_module(auth)

DISCOVERY = {"issuer": auth.ISSUER, "authorization_endpoint": auth.ISSUER + "/authorize",
             "token_endpoint": auth.ISSUER + "/token", "jwks_uri": auth.ISSUER + "/jwks",
             "revocation_endpoint": auth.ISSUER + "/revoke"}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "test-key", "alg": "RS256"}


class Store:
    def __init__(self, record=None):
        self.record = copy.deepcopy(record)
        self.saves = []

    async def async_load(self):
        return copy.deepcopy(self.record)

    async def async_save(self, record):
        self.record = copy.deepcopy(record)
        self.saves.append(copy.deepcopy(record))


class Response:
    def __init__(self, data, status=200, wait=None, entered=None):
        self.status = status
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.content = self
        self.wait = wait
        self.entered = entered

    async def __aenter__(self):
        if self.entered:
            self.entered.set()
        if self.wait:
            await self.wait.wait()
        return self

    async def __aexit__(self, *_):
        return False

    async def iter_chunked(self, size):
        for index in range(0, len(self.body), size):
            yield self.body[index:index + size]


class Session:
    def __init__(self):
        self.tokens = []
        self.calls = []
        self.jwks_failure = False
        self.revoke_failure = False
        self.discovery = copy.deepcopy(DISCOVERY)
        # Official SIWC /v1/models response, distinct from API-key catalogs.
        self.catalog = {"models": [{"slug": "model-one", "display_name": "model-one", "visibility": "list"},
                                   {"slug": "model-two", "display_name": "Second", "visibility": "list", "secret": "SECRET"},
                                   {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide"}]}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("openid-configuration"):
            return Response(self.discovery)
        if url.endswith("jwks"):
            if self.jwks_failure:
                raise aiohttp.ClientConnectionError("SECRET provider failure")
            return Response({"keys": [JWK]})
        if url.endswith("token"):
            value = self.tokens.pop(0)
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, Response) else Response(value)
        if url.endswith("revoke"):
            return Response({}, 503 if self.revoke_failure else 200)
        if url.endswith("models"):
            return Response(self.catalog)
        raise AssertionError("Unexpected endpoint")


def setup(record=None):
    clock = [time.time()]
    store, session = Store(record), Session()
    manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
    return manager, store, session, clock


def token(now, nonce=None, claims=None, **fields):
    payload = {"iss": auth.ISSUER, "aud": "issued-client", "sub": "subject-one", "iat": now,
               "exp": now + 3600, "email": "user@example.test", "name": "Example"}
    if nonce is not None:
        payload["nonce"] = nonce
    payload.update(claims or {})
    return {"access_token": "SECRET-access", "refresh_token": "SECRET-refresh", "expires_in": 3600,
            "token_type": "Bearer", "scope": auth.SCOPES,
            "id_token": jwt.encode(payload, KEY, algorithm="RS256", headers={"kid": "test-key"}), **fields}


def callback(start, **params):
    query = parse_qs(urlsplit(start["auth_url"]).query)
    values = {"state": query["state"][0], "code": "SECRET-code", "client_id": "issued-client", **params}
    return auth.REDIRECT_URI + "?" + urlencode(values)


async def connect(manager, session, clock, **fields):
    start = await manager.async_start_login("admin")
    params = parse_qs(urlsplit(start["auth_url"]).query)
    session.tokens.append(token(clock[0], params["nonce"][0], **fields))
    await manager.async_complete_login(callback(start), start["login_id"], "admin")
    return start


def test_browser_pkce_verified_identity_private_status_and_registration_reuse():
    async def run():
        manager, store, session, clock = setup()
        start = await connect(manager, session, clock)
        query = parse_qs(urlsplit(start["auth_url"]).query)
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["ext_agent_host_id"][0].startswith("urn:uuid:")
        assert query["resource"] == [auth.RESOURCE]
        assert set(query["scope"][0].split()) == auth.REQUIRED_SCOPES
        assert query["code_challenge_method"] == ["S256"]
        status = manager.status()
        assert status["connected"] and status["account"]["email"] == "user@example.test"
        assert "SECRET" not in json.dumps(status)
        assert "id_token" not in json.dumps(store.record)
        assert await manager.async_access_token() == "SECRET-access"
        assert await manager.async_models() == [{"id": "model-one", "name": "model-one"}, {"id": "model-two", "name": "Second"}]
        host_id = store.record["host_id"]
        restored = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        next_start = await restored.async_start_login("admin")
        query = parse_qs(urlsplit(next_start["auth_url"]).query)
        assert query["client_id"] == ["issued-client"]
        assert query["ext_agent_host_id"] == [host_id]
        assert "agent_name_hint" not in query
        for _, _, args in session.calls:
            assert args["allow_redirects"] is False
            assert args["timeout"].total == 20
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["state", "owner", "login_id", "origin", "fragment", "duplicate", "client", "code"])
def test_invalid_callback_cannot_exchange_or_consume_pending(mutation):
    async def run():
        manager, store, session, clock = setup()
        start = await manager.async_start_login("admin")
        url, owner, login_id = callback(start), "admin", start["login_id"]
        if mutation == "state":
            url = callback(start, state="wrong")
        if mutation == "owner":
            owner = "other-admin"
        if mutation == "login_id":
            login_id = "wrong"
        if mutation == "origin":
            url = url.replace("127.0.0.1", "evil.example")
        if mutation == "fragment":
            url += "#secret"
        if mutation == "duplicate":
            url += "&state=other"
        if mutation == "client":
            url = callback(start, client_id="dynamic_agent_client")
        if mutation == "code":
            url = callback(start, code="")
        with pytest.raises(auth.ChatGPTAuthError):
            await manager.async_complete_login(url, login_id, owner)
        assert not any(url.endswith("/token") for _, url, _ in session.calls)
        assert manager.status()["pending"]
        assert "credentials" not in store.record
    asyncio.run(run())


@pytest.mark.parametrize("claims", [{"nonce": "wrong"}, {"iss": "https://evil.example"}, {"aud": "other-client"},
                                    {"exp": 0}, {"iat": 9999999999}, {"sub": ""}, {"azp": "other-client"}])
def test_invalid_id_claims_never_connect(claims):
    async def run():
        manager, store, session, clock = setup()
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await connect(manager, session, clock, claims=claims)
        assert failure.value.code == "invalid_id_token"
        assert not manager.status()["connected"]
        assert "credentials" not in store.record
        assert store.record["client_id"] == "issued-client"
    asyncio.run(run())


@pytest.mark.parametrize("fields", [{"id_token": "invalid.SECRET"}, {"scope": "openid profile email offline_access resource.invoke"},
                                    {"token_type": "mac"}, {"refresh_token": ""}, {"expires_in": True}, {"expires_in": -1}])
def test_invalid_token_contract_never_persists_credentials(fields):
    async def run():
        manager, store, session, clock = setup()
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await connect(manager, session, clock, **fields)
        assert "SECRET" not in str(failure.value)
        assert "credentials" not in store.record
    asyncio.run(run())


def test_forged_signature_rejected():
    async def run():
        manager, store, session, clock = setup()
        start = await manager.async_start_login("admin")
        nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
        value = token(clock[0], nonce)
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = jwt.decode(value["id_token"], options={"verify_signature": False})
        value["id_token"] = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "test-key"})
        session.tokens.append(value)
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_complete_login(callback(start), start["login_id"], "admin")
        assert failure.value.code == "invalid_id_token"
        assert "credentials" not in store.record
    asyncio.run(run())


def test_expiry_cancellation_and_denial_are_bound_to_owner():
    async def run():
        manager, store, session, clock = setup()
        start = await manager.async_start_login("admin")
        with pytest.raises(auth.ChatGPTAuthError):
            await manager.async_start_login("other")
        with pytest.raises(auth.ChatGPTAuthError):
            await manager.async_cancel_login(start["login_id"], "other")
        clock[0] += 601
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_complete_login(callback(start), start["login_id"], "admin")
        assert failure.value.code == "login_expired"
        start = await manager.async_start_login("admin")
        await manager.async_cancel_login(start["login_id"], "admin")
        assert manager.status()["pending"] is None
        start = await manager.async_start_login("admin")
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_complete_login(callback(start, error="SECRET-provider-detail"), start["login_id"], "admin")
        assert failure.value.code == "authorization_denied"
        assert "SECRET" not in str(failure.value)
        assert manager.status()["pending"] is None
    asyncio.run(run())


def test_cancel_inflight_exchange_cannot_install_credentials():
    async def run():
        manager, store, session, clock = setup()
        start = await manager.async_start_login("admin")
        nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
        release = asyncio.Event()
        session.tokens.append(Response(token(clock[0], nonce), wait=release))
        completion = asyncio.create_task(manager.async_complete_login(callback(start), start["login_id"], "admin"))
        await asyncio.sleep(0)
        await manager.async_cancel_login(start["login_id"], "admin")
        release.set()
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await completion
        assert failure.value.code == "cancelled"
        assert "credentials" not in store.record
    asyncio.run(run())


@pytest.mark.parametrize("save_fails", [False, True])
def test_cancel_waits_for_final_login_save(save_fails):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedStore(Store):
            async def async_save(self, record):
                if record.get("status") == "connected":
                    entered.set()
                    await release.wait()
                    if save_fails:
                        raise OSError("SECRET storage failure")
                await super().async_save(record)

        manager, _, session, clock = setup()
        store = PausedStore()
        manager._store = store
        start = await manager.async_start_login("admin")
        nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
        session.tokens.append(token(clock[0], nonce))
        completion = asyncio.create_task(manager.async_complete_login(callback(start), start["login_id"], "admin"))
        await entered.wait()
        cancellation = asyncio.create_task(manager.async_cancel_login(start["login_id"], "admin"))
        # Let the caller and its owned operation reach the paused commit.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not cancellation.done()
        release.set()
        if save_fails:
            with pytest.raises(auth.ChatGPTAuthError) as failure:
                await completion
            assert failure.value.code == "storage_unavailable"
        else:
            assert (await completion)["connected"]
        cancelled_status = await cancellation
        assert cancelled_status["connected"] is (not save_fails)
        assert cancelled_status == manager.status()
        assert ("credentials" in store.record) is (not save_fails)
    asyncio.run(run())


@pytest.mark.parametrize("login_id", ["é", "登录", "\ud800"])
@pytest.mark.parametrize("operation", ["complete", "cancel", "restart_required"])
def test_non_ascii_login_id_is_sanitized(login_id, operation):
    async def run():
        manager, store, session, clock = setup()
        start = await manager.async_start_login("admin")
        if operation == "restart_required":
            assert manager.login_restart_required(login_id, "admin")
        else:
            with pytest.raises(auth.ChatGPTAuthError) as failure:
                if operation == "complete":
                    await manager.async_complete_login(callback(start), login_id, "admin")
                else:
                    await manager.async_cancel_login(login_id, "admin")
            assert failure.value.code == "login_mismatch"
            assert login_id not in failure.value.message
        assert manager.status()["pending"]["login_id"] == start["login_id"]
        assert not any(url.endswith("/token") for _, url, _ in session.calls)
    asyncio.run(run())


@pytest.mark.parametrize("provider_error", ["invalid_client", "unauthorized_client", "invalid_grant"])
@pytest.mark.parametrize("grant", ["authorization_code", "refresh_token"])
def test_rejected_registration_is_retired_for_next_login(provider_error, grant):
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        host_id = store.record["host_id"]
        session.tokens.append(Response({"error": provider_error, "error_description": "SECRET"}, 400))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            if grant == "authorization_code":
                start = await manager.async_start_login("admin")
                await manager.async_complete_login(callback(start), start["login_id"], "admin")
            else:
                await manager.async_access_token(force_refresh=True)
        assert failure.value.code == provider_error
        retired = provider_error != "invalid_grant"
        assert ("client_id" not in store.record) is retired
        if retired:
            assert "credentials" not in store.record
            assert "pending_refresh" not in store.record
        restored = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        next_login = await restored.async_start_login("admin")
        params = parse_qs(urlsplit(next_login["auth_url"]).query)
        assert params["client_id"] == ["dynamic_agent_client" if retired else "issued-client"]
        assert ("agent_name_hint" in params) is retired
        assert params["ext_agent_host_id"] == [host_id]
    asyncio.run(run())


def test_concurrent_refresh_consumes_one_grant_and_persists_rotation():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        clock[0] += 3500
        session.tokens.append(token(clock[0], access_token="SECRET-new-access", refresh_token="SECRET-new-refresh"))
        values = await asyncio.gather(*(manager.async_access_token() for _ in range(12)))
        assert values == ["SECRET-new-access"] * 12
        refreshes = [args["data"] for _, url, args in session.calls if url.endswith("token") and args["data"]["grant_type"] == "refresh_token"]
        assert len(refreshes) == 1 and refreshes[0]["refresh_token"] == "SECRET-refresh"
        assert store.record["credentials"]["refresh_token"] == "SECRET-new-refresh"
        assert "pending_refresh" not in store.record
        assert any("pending_refresh" in record for record in store.saves)
        assert "SECRET" not in json.dumps(manager.status())
    asyncio.run(run())


def test_shutdown_drains_rotation_before_replacement_and_retires_old_manager():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        clock[0] += 3500
        entered, release, shutdown_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        session.tokens.append(Response(token(clock[0], access_token="SECRET-rotated", refresh_token="SECRET-rotated-refresh"),
                                       wait=release, entered=entered))
        refresh = asyncio.create_task(manager.async_access_token())
        await entered.wait()

        async def retire():
            shutdown_started.set()
            await manager.async_shutdown()

        shutdown = asyncio.create_task(retire())
        await shutdown_started.wait()
        try:
            assert not shutdown.done(), "Replacement must wait until the old grant rotation finishes"
        finally:
            release.set()
            await asyncio.gather(refresh, shutdown)
        assert await refresh == "SECRET-rotated"
        assert manager.status()["pending"] is None
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert replacement.status()["connected"]
        assert await replacement.async_access_token() == "SECRET-rotated"
        for operation in (manager.async_load, manager.async_access_token,
                          lambda: manager.async_start_login("admin"), manager.async_logout):
            with pytest.raises(auth.ChatGPTAuthError) as failure:
                await operation()
            assert failure.value.code == "integration_unavailable"
        assert await replacement.async_access_token() == "SECRET-rotated"
    asyncio.run(run())


def test_cancelled_refresh_and_shutdown_drain_storage_before_replacement():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedRotationStore(Store):
            async def async_save(self, record):
                if record.get("pending_refresh") and not entered.is_set():
                    entered.set()
                    await release.wait()
                await super().async_save(record)

        clock = [time.time()]
        store, session = PausedRotationStore(), Session()
        manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(manager, session, clock)
        clock[0] += 3500
        session.tokens.append(token(clock[0], access_token="SECRET-rotated", refresh_token="SECRET-rotated-refresh"))
        refresh = asyncio.create_task(manager.async_access_token())
        await entered.wait()
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        queued_load = asyncio.create_task(manager.async_load())
        queued_started = asyncio.Event()
        asyncio.get_running_loop().call_soon(queued_started.set)
        await queued_started.wait()
        shutdown_started = asyncio.Event()

        async def retire():
            shutdown_started.set()
            await manager.async_shutdown()

        shutdown = asyncio.create_task(retire())
        await shutdown_started.wait()
        shutdown.cancel()
        cancellation_processed = asyncio.Event()
        asyncio.get_running_loop().call_soon(cancellation_processed.set)
        await cancellation_processed.wait()
        try:
            assert not shutdown.done(), "Cancelled shutdown must still await durable grant storage"
        finally:
            release.set()
            results = await asyncio.gather(shutdown, queued_load, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert isinstance(results[1], auth.ChatGPTAuthError)
        assert results[1].code == "integration_unavailable"
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert replacement.status()["connected"]
        assert await replacement.async_access_token() == "SECRET-rotated"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "integration_unavailable"
    asyncio.run(run())


def test_transient_refresh_retains_credentials_terminal_refresh_clears():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        before = copy.deepcopy(store.record)
        clock[0] += 3600
        session.tokens.append(aiohttp.ClientConnectionError("SECRET-error"))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "network_error" and "SECRET" not in str(failure.value)
        assert store.record == before
        session.tokens.append(Response({"error": "invalid_grant", "error_description": "SECRET-details"}, 400))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "invalid_grant"
        assert manager.status()["status"] == "reconnect_required"
        assert "credentials" not in store.record
        assert store.record["client_id"] == "issued-client"
    asyncio.run(run())


def test_checkpoint_survives_key_outage_and_restart_without_reusing_grant():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        clock[0] += 3700
        manager._jwks = None
        session.jwks_failure = True
        session.tokens.append(token(clock[0], access_token="SECRET-rotated", refresh_token="SECRET-rotated-refresh"))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "identity_verification_unavailable"
        assert store.record["pending_refresh"]["credentials"]["refresh_token"] == "SECRET-rotated-refresh"
        assert store.record["credentials"]["refresh_token"] == "SECRET-refresh"
        session.jwks_failure = False
        restored = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        assert await restored.async_access_token() == "SECRET-rotated"
        assert not session.tokens
        refreshes = [args for _, url, args in session.calls if url.endswith("token") and args["data"]["grant_type"] == "refresh_token"]
        assert len(refreshes) == 1
    asyncio.run(run())


def test_forced_401_refresh_deduplicates_by_rejected_token():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        session.tokens.append(token(clock[0], access_token="SECRET-new", refresh_token="SECRET-new-refresh"))
        values = await asyncio.gather(*(manager.async_access_token(True, "SECRET-access") for _ in range(8)))
        assert values == ["SECRET-new"] * 8
        assert not session.tokens
    asyncio.run(run())


def test_logout_clears_even_if_revoke_fails_preserving_registration():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        host_id = store.record["host_id"]
        session.revoke_failure = True
        result = await manager.async_logout()
        assert not result["connected"] and "warning" in result
        assert "SECRET" not in json.dumps(result)
        assert store.record == {"host_id": host_id, "client_id": "issued-client", "status": "disconnected", "remote_disconnection_pending": True}
        revocation = next(args["data"] for _, url, args in session.calls if url.endswith("revoke"))
        assert revocation["token"] == "SECRET-refresh" and revocation["token_type_hint"] == "refresh_token"
    asyncio.run(run())


def test_remote_logout_warning_survives_status_and_reload_until_new_connection():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        session.revoke_failure = True
        result = await manager.async_logout()
        warning = "Local credentials were removed. Remote disconnection could not be confirmed; disconnect PowerSync in ChatGPT Settings."
        assert result["warning"] == warning and not result["connected"]
        assert manager.status().get("warning") == warning, "A fresh GET must recover the failed remote-cleanup notice"
        await manager.async_shutdown()
        restored = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await restored.async_load()
        assert restored.status().get("warning") == warning
        assert (await restored.async_logout()).get("warning") == warning
        await connect(restored, session, clock, claims={"sub": "subject-new", "email": "new@example.test"})
        assert restored.status()["connected"] and "warning" not in restored.status()
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert replacement.status()["connected"] and "warning" not in replacement.status()
        session.revoke_failure = False
        assert "warning" not in await replacement.async_logout()
        assert "warning" not in replacement.status()
        assert "SECRET" not in json.dumps(restored.status())
    asyncio.run(run())


@pytest.mark.parametrize("revoke_failure", [False, True])
def test_failed_logout_save_retires_credentials_and_blocks_handoff(revoke_failure):
    async def run():
        class RecoverableStore(Store):
            available = True

            async def async_save(self, record):
                if not self.available:
                    raise OSError("SECRET-storage-details")
                await super().async_save(record)

        clock = [time.time()]
        session, store = Session(), RecoverableStore()
        manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(manager, session, clock)
        session.revoke_failure = revoke_failure
        store.available = False
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_logout()
        assert failure.value.code == "storage_unavailable" and "SECRET" not in str(failure.value)
        status = manager.status()
        assert not status["connected"], "A completed logout attempt must retire credentials despite a failed save"
        assert status["status"] == "disconnected" and status["account"] is None
        assert "warning" not in status  # Do not claim durable local removal before the write succeeds.
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "not_connected"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_load()
        assert failure.value.code == "storage_unavailable"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_shutdown()
        assert failure.value.code == "storage_unavailable"
        store.available = True
        await manager.async_shutdown()
        session.tokens.append(Response({"error": "invalid_grant"}, 400))
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert not replacement.status()["connected"]
        assert ("warning" in replacement.status()) is revoke_failure
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await replacement.async_access_token()
        assert failure.value.code == "not_connected"
        session.tokens.clear()  # The spent grant was never requested; supply a fresh sign-in response next.
        await connect(replacement, session, clock, claims={"sub": "subject-new", "email": "new@example.test"})
        assert replacement.status()["account"]["email"] == "new@example.test"
        assert "warning" not in replacement.status()
    asyncio.run(run())


def test_untrusted_discovery_and_oversize_response_rejected():
    async def run():
        manager, store, session, clock = setup()
        session.discovery["token_endpoint"] = "https://auth.openai.com@evil.example/token"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_start_login("admin")
        assert failure.value.code == "discovery_failed"
        session.discovery = copy.deepcopy(DISCOVERY)
        start = await manager.async_start_login("admin")
        session.tokens.append(Response(b"X" * (auth.MAX_RESPONSE_BYTES + 1)))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_complete_login(callback(start), start["login_id"], "admin")
        assert failure.value.code == "invalid_response"
    asyncio.run(run())


def test_refresh_without_id_token_or_scope_preserves_verified_identity():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        clock[0] += 3600
        refreshed = token(clock[0], access_token="SECRET-new", refresh_token="SECRET-new-refresh")
        refreshed.pop("id_token")
        refreshed.pop("scope")
        session.tokens.append(refreshed)
        assert await manager.async_access_token() == "SECRET-new"
        assert manager.status()["account"]["email"] == "user@example.test"
        assert set(store.record["scopes"]) == auth.REQUIRED_SCOPES
    asyncio.run(run())


def test_replacement_account_mismatch_preserves_old_verified_credentials():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        before = copy.deepcopy(store.record)
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await connect(manager, session, clock, claims={"sub": "subject-other"})
        assert failure.value.code == "account_mismatch"
        assert store.record == before
        assert manager.status()["connected"]
    asyncio.run(run())


def test_refresh_account_mismatch_requires_reconnect():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        clock[0] += 3600
        session.tokens.append(token(clock[0], claims={"sub": "subject-other"}))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "account_mismatch"
        assert not manager.status()["connected"]
        assert "credentials" not in store.record and "pending_refresh" not in store.record
    asyncio.run(run())


@pytest.mark.parametrize("bad_field", [{"scopes": 123}, {"credentials": {"access_token": "SECRET", "refresh_token": "SECRET", "expires_at": True}},
                                      {"pending_refresh": {"credentials": "SECRET"}}])
def test_malformed_persisted_credentials_require_reconnect(bad_field):
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        store.record.update(bad_field)
        restored = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await restored.async_load()
        assert not restored.status()["connected"]
        assert "credentials" not in store.record and "pending_refresh" not in store.record
    asyncio.run(run())


def test_storage_outage_after_refresh_never_reuses_consumed_grant():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        clock[0] += 3600
        save = store.async_save
        async def fail_save(record):
            raise OSError("SECRET-storage-error")
        store.async_save = fail_save
        session.tokens.append(token(clock[0], access_token="SECRET-new", refresh_token="SECRET-new-refresh"))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "storage_unavailable" and "SECRET" not in str(failure.value)
        store.async_save = save
        assert await manager.async_access_token() == "SECRET-new"
        refreshes = [args for _, url, args in session.calls if url.endswith("token") and args["data"]["grant_type"] == "refresh_token"]
        assert len(refreshes) == 1
        assert store.record["credentials"]["refresh_token"] == "SECRET-new-refresh"
    asyncio.run(run())


def test_shutdown_persists_retained_rotation_after_storage_recovers():
    async def run():
        class RecoverableStore(Store):
            available = True

            async def async_save(self, record):
                if not self.available:
                    raise OSError("SECRET-storage-details")
                await super().async_save(record)

        clock = [time.time()]
        session, store = Session(), RecoverableStore()
        manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(manager, session, clock)
        clock[0] += 3500
        store.available = False
        session.tokens.append(token(clock[0], access_token="SECRET-rotated", refresh_token="SECRET-rotated-refresh"))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "storage_unavailable"
        store.available = True
        await manager.async_shutdown()
        # A provider rejects reuse of the grant already consumed by the old manager.
        session.tokens.append(Response({"error": "invalid_grant"}, 400))
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert replacement.status()["connected"]
        assert await replacement.async_access_token() == "SECRET-rotated"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "integration_unavailable"
    asyncio.run(run())


def test_shutdown_fails_safely_until_retained_rotation_can_be_saved():
    async def run():
        class RecoverableStore(Store):
            available = True

            async def async_save(self, record):
                if not self.available:
                    raise OSError("SECRET-storage-details")
                await super().async_save(record)

        clock = [time.time()]
        session, store = Session(), RecoverableStore()
        manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(manager, session, clock)
        clock[0] += 3500
        store.available = False
        session.tokens.append(token(clock[0], access_token="SECRET-rotated", refresh_token="SECRET-rotated-refresh"))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "storage_unavailable"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_shutdown()
        assert failure.value.code == "storage_unavailable" and failure.value.http_status == 503
        assert "SECRET" not in str(failure.value)
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "integration_unavailable"
        store.available = True
        await manager.async_shutdown()
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert replacement.status()["connected"]
        assert await replacement.async_access_token() == "SECRET-rotated"
    asyncio.run(run())


@pytest.mark.parametrize("refresh_failure", ["missing_scope", "invalid_token_response", "invalid_grant"])
def test_failed_terminal_disconnect_cannot_handoff_or_reuse_consumed_grant(refresh_failure):
    async def run():
        class RecoverableStore(Store):
            available = True

            async def async_save(self, record):
                if not self.available:
                    raise OSError("SECRET-storage-details")
                await super().async_save(record)

        clock = [time.time()]
        session, store = Session(), RecoverableStore()
        manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(manager, session, clock)
        clock[0] += 3500
        store.available = False
        if refresh_failure == "invalid_grant":
            session.tokens.append(Response({"error": "invalid_grant"}, 400))
        else:
            fields = {"scope": "openid"} if refresh_failure == "missing_scope" else {"token_type": "mac"}
            session.tokens.append(token(clock[0], access_token="SECRET-rotated", refresh_token="SECRET-rotated-refresh", **fields))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "storage_unavailable" and "SECRET" not in str(failure.value)
        status = manager.status()
        assert not status["connected"], "A rejected refresh must retire the consumed grant even when storage fails"
        assert status["status"] == "reconnect_required" and status["account"] is None
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "not_connected"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_shutdown()
        assert failure.value.code == "storage_unavailable" and "SECRET" not in str(failure.value)
        store.available = True
        await manager.async_shutdown()
        session.tokens.append(Response({"error": "invalid_grant"}, 400))
        replacement = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await replacement.async_load()
        assert replacement.status()["status"] == "reconnect_required"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await replacement.async_access_token()
        assert failure.value.code == "not_connected"
    asyncio.run(run())


def test_load_retry_keeps_dirty_reconnect_state_while_storage_is_unavailable():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class RecoverableStore(Store):
            available = True
            pause = False

            async def async_save(self, record):
                if self.pause:
                    entered.set()
                    await release.wait()
                if not self.available:
                    raise OSError("SECRET-storage-details")
                await super().async_save(record)

        clock = [time.time()]
        session, store = Session(), RecoverableStore()
        original = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        await connect(original, session, clock)
        await original.async_shutdown()
        store.record["credentials"]["expires_at"] = True  # Invalid durable credential input.
        store.available = False
        manager = auth.ChatGPTAuthManager(session, store, lambda: clock[0])
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_load()
        assert failure.value.code == "storage_unavailable"
        assert manager.status()["status"] == "reconnect_required"
        store.pause = True
        retry = asyncio.create_task(manager.async_load())
        await entered.wait()
        try:
            assert not manager.status()["connected"], "A load retry must not restore the retired durable record while saving"
            assert manager.status()["status"] == "reconnect_required"
        finally:
            release.set()
            result = (await asyncio.gather(retry, return_exceptions=True))[0]
        assert isinstance(result, auth.ChatGPTAuthError) and result.code == "storage_unavailable"
        store.available = True
        await manager.async_load()
        assert manager.status()["status"] == "reconnect_required"
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "not_connected"
    asyncio.run(run())


def test_unused_chatgpt_load_does_not_require_a_storage_write(tmp_path):
    async def run():
        class Hass:
            async def async_add_executor_job(self, function, *args):
                return function(*args)

        class DiskStore:
            path = tmp_path / "credentials"

            async def async_load(self):
                return None

            async def async_save(self, record):
                pass  # HA's Store can swallow a write error.

        session = Session()
        store = DiskStore()
        manager = auth.ChatGPTAuthManager(session, auth._PrivateCredentialStore(Hass(), store))
        await manager.async_load()
        await manager.async_load()
        assert manager.status()["status"] == "disconnected"
        assert not manager.status()["connected"]
        assert not store.path.exists()
        assert session.calls == []
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_access_token()
        assert failure.value.code == "not_connected"
        # A sign-in must still durably save its host identity before contacting
        # the provider or exposing a URL; an unused feature needs no such write.
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_start_login("admin")
        assert failure.value.code == "storage_unavailable"
        assert manager.status()["pending"] is None
        assert session.calls == []
        await manager.async_shutdown()
        assert not store.path.exists()

    asyncio.run(run())


def test_first_sign_in_persists_host_identity_after_startup_and_save_retry():
    async def run():
        class RecoverableStore(Store):
            available = False

            async def async_save(self, record):
                if not self.available:
                    raise OSError("SECRET-storage-details")
                await super().async_save(record)

        store, session = RecoverableStore(), Session()
        manager = auth.ChatGPTAuthManager(session, store)
        await manager.async_load()
        assert store.record is None and store.saves == []
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_start_login("admin")
        assert failure.value.code == "storage_unavailable"
        assert "SECRET" not in str(failure.value)
        store.available = True
        start = await manager.async_start_login("admin")
        host_id = parse_qs(urlsplit(start["auth_url"]).query)["ext_agent_host_id"][0]
        assert store.record["host_id"] == host_id
        replacement = auth.ChatGPTAuthManager(session, store)
        await replacement.async_load()
        restarted = await replacement.async_start_login("admin")
        assert parse_qs(urlsplit(restarted["auth_url"]).query)["ext_agent_host_id"] == [host_id]

    asyncio.run(run())


def test_private_store_rejects_silent_failure_and_unsafe_permissions(tmp_path):
    async def run():
        path = tmp_path / "credentials"
        record = {"access_token": "SECRET"}
        class Hass:
            async def async_add_executor_job(self, function, *args):
                return function(*args)
        class DiskStore:
            async def async_load(self):
                return None
            async def async_save(self, data):
                pass  # Home Assistant can swallow a write error.
        store = DiskStore()
        store.path = path
        wrapper = auth._PrivateCredentialStore(Hass(), store)
        with pytest.raises(OSError):
            await wrapper.async_save(record)
        path.write_text(json.dumps({"data": record}))
        path.chmod(0o644)
        with pytest.raises(OSError, match="permissions"):
            await wrapper.async_save(record)
        path.chmod(0o600)
        await wrapper.async_save(record)
        with pytest.raises(OSError) as failure:
            await wrapper.async_save({"access_token": "SECRET-new"})
        assert "SECRET" not in str(failure.value)
    asyncio.run(run())


def test_subscription_catalog_visible_valid_slugs_only_preserves_order():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        session.catalog = {"models": [
            {"slug": "gpt-second", "display_name": "Second", "visibility": "list"},
            {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
            {"slug": "gpt-no-visibility", "display_name": "Unknown"},
            {"slug": "bad slug", "display_name": "Invalid", "visibility": "list"},
            {"slug": "gpt-💣", "display_name": "Unicode slug", "visibility": "list"},
            {"slug": "x" * 129, "visibility": "list"},
            {"slug": "gpt\nsecret", "visibility": "list"},
            {"slug": "", "visibility": "list"},
            {"slug": 42, "visibility": "list"},
            {"slug": "gpt-first", "display_name": "First", "visibility": "list"},
            {"slug": "gpt-second", "display_name": "Duplicate", "visibility": "list"},
            {"slug": "gpt-fallback", "display_name": "X" * 257, "visibility": "list"},
            {"slug": "gpt-control-name", "display_name": "unsafe\nname", "visibility": "list"},
            {"slug": "gpt-empty-name", "display_name": "  ", "visibility": "list"},
            "invalid row", None,
        ], "data": [{"id": "ignored-api-catalog"}]}
        assert await manager.async_models() == [
            {"id": "gpt-second", "name": "Second"}, {"id": "gpt-first", "name": "First"},
            {"id": "gpt-fallback", "name": "gpt-fallback"}, {"id": "gpt-control-name", "name": "gpt-control-name"},
            {"id": "gpt-empty-name", "name": "gpt-empty-name"}]
        request = next(args for _, url, args in session.calls if url.endswith("models"))
        assert request["headers"]["Authorization"] == "Bearer SECRET-access"
    asyncio.run(run())


def test_compatible_api_catalog_validated_without_subscription_visibility_requirement():
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        session.catalog = {"data": [{"id": "gpt-api", "name": "API model"}, {"id": "gpt-default-name"},
                                    {"id": "invalid model"}, {"id": "gpt-hidden", "visibility": "hide"}]}
        assert await manager.async_models() == [{"id": "gpt-api", "name": "API model"},
                                               {"id": "gpt-default-name", "name": "gpt-default-name"}]
    asyncio.run(run())


@pytest.mark.parametrize("catalog", [{}, {"models": None}, {"models": {}}, {"models": "SECRET"},
                                     {"models": None, "data": [{"id": "gpt-ignore"}]}, {"data": {}}])
def test_malformed_catalog_has_safe_error_without_disconnect(catalog):
    async def run():
        manager, store, session, clock = setup()
        await connect(manager, session, clock)
        session.catalog = catalog
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_models()
        assert failure.value.code == "invalid_response"
        assert "SECRET" not in str(failure.value)
        assert manager.status()["connected"]
    asyncio.run(run())


@pytest.mark.parametrize("exception,category", [
    (asyncio.TimeoutError("SECRET-timeout-with-callback-code"), "timeout"),
    (aiohttp.ClientConnectorCertificateError(None, ssl.SSLCertVerificationError(1, "SECRET-certificate-url")), "tls"),
    (aiohttp.ClientConnectorSSLError(None, ssl.SSLError(1, "SECRET-handshake-url")), "tls"),
    (getattr(aiohttp, "ClientConnectorDNSError", aiohttp.ClientConnectorError)(None, OSError(-2, "SECRET-dns-host")),
     "dns" if hasattr(aiohttp, "ClientConnectorDNSError") else "connection"),
    (aiohttp.ClientResponseError(None, (), status=500, message="SECRET-response", headers={"Authorization": "SECRET-token"}), "http_transport"),
    (aiohttp.ClientPayloadError("SECRET-provider-body"), "response_body"),
    (aiohttp.ServerDisconnectedError("SECRET-response-header"), "connection"),
    (aiohttp.ClientConnectorError(None, OSError(111, "SECRET-token-and-auth-url")), "connection"),
    (OSError(101, "SECRET-auth-url-and-authorization-code"), "socket"),
    (OSError(999999, "SECRET-nonstandard-errno"), "socket"),
])
def test_transport_diagnostics_never_log_provider_or_credential_details(caplog, exception, category):
    async def run():
        manager, store, session, clock = setup()
        session.tokens.append(exception)
        with caplog.at_level("WARNING", logger=auth.__name__):
            with pytest.raises(auth.ChatGPTAuthError) as failure:
                await manager._request("POST", DISCOVERY["token_endpoint"],
                                       data={"grant_type": "authorization_code", "code": "SECRET-code", "code_verifier": "SECRET-verifier"},
                                       token="SECRET-access-token")
        assert failure.value.code == "network_error"
        messages = [record.getMessage() for record in caplog.records if record.name == auth.__name__]
        assert messages and "operation=authorization_code_exchange" in messages[-1]
        assert f"category={category}" in messages[-1]
        assert "SECRET" not in caplog.text and "https://" not in caplog.text
        assert "Traceback" not in caplog.text
        if getattr(exception, "errno", None) == 999999:
            assert "errno=None" in messages[-1] and "999999" not in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize("provider_error", ["invalid_client", "unauthorized_client"])
@pytest.mark.parametrize("save_fails", [False, True])
def test_first_login_rejected_client_stays_disconnected_and_preserves_error(provider_error, save_fails):
    async def run():
        manager, store, session, clock = setup()
        start = await manager.async_start_login("admin")
        original_save = store.async_save
        async def save(record):
            if save_fails and record.get("status") == "disconnected":
                raise OSError("SECRET-storage")
            await original_save(record)
        store.async_save = save
        session.tokens.append(Response({"error": provider_error}, 400))
        with pytest.raises(auth.ChatGPTAuthError) as failure:
            await manager.async_complete_login(callback(start), start["login_id"], "admin")
        assert failure.value.code == provider_error
        assert manager.status()["status"] == "disconnected"
        assert "client_id" not in manager._record
        assert manager._record_needs_save is save_fails
    asyncio.run(run())


@pytest.mark.parametrize("record_kind", ["rotation", "unused", "warning", "missing_endpoint"])
def test_removal_revokes_retained_rotation_after_shutdown_without_saving(record_kind):
    async def run():
        manager, store, session, clock = setup()
        if record_kind != "unused":
            await connect(manager, session, clock)
            manager._record["pending_refresh"] = {"credentials": {"refresh_token": "SECRET-newest"}}
        await manager.async_shutdown()
        if record_kind == "warning":
            session.revoke_failure = True
        if record_kind == "missing_endpoint":
            manager._discovery.pop("revocation_endpoint", None)
        saves = len(store.saves)
        result = await manager.async_revoke_for_removal()
        assert len(store.saves) == saves
        assert bool(result.get("warning")) is (record_kind in {"warning", "missing_endpoint"})
        revocations = [args for _, url, args in session.calls if url.endswith("revoke")]
        if record_kind in {"rotation", "warning"}:
            assert revocations[0]["data"]["token"] == "SECRET-newest"
        else:
            assert not revocations
    asyncio.run(run())
