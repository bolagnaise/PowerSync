"""Exercise owner consent, temporary account cleanup and the admin MCP transport."""
import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import ssl
import sys
from types import ModuleType, SimpleNamespace

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from test_support_ha_runtime import MemoryStore

ROOT = Path(__file__).parents[1]
OWNER = SimpleNamespace(is_owner=True, id="owner")
ADMIN = SimpleNamespace(is_owner=False, id="ordinary-admin")
VERIFIER = "a" * 64
CHALLENGE = sha256(VERIFIER.encode()).hexdigest()


class Auth:
    def __init__(self):
        self.users = {}
        self.tokens = {}

    async def async_get_users(self):
        return list(self.users.values())

    async def async_get_user(self, user_id):
        return self.users.get(user_id)

    async def async_create_system_user(self, name, *, group_ids):
        user = SimpleNamespace(id=name, name=name, is_owner=False, system_generated=True,
                               groups=group_ids, refresh_tokens={})
        self.users[user.id] = user
        return user

    async def async_create_refresh_token(self, user, *, access_token_expiration):
        assert timedelta(0) < access_token_expiration <= timedelta(minutes=30)
        token = SimpleNamespace(user=user, id=user.id)
        user.refresh_tokens[token.id] = token
        self.tokens[token.id] = token
        return token

    def async_create_access_token(self, refresh):
        return refresh.id

    def async_remove_refresh_token(self, token):
        del self.tokens[token.id]
        del token.user.refresh_tokens[token.id]

    async def async_remove_user(self, user):
        assert not user.refresh_tokens
        del self.users[user.id]


class AccountStore(MemoryStore):
    value = None

    def __init__(self, *args, **kwargs):
        self.value = None

    async def async_remove(self):
        self.value = None


@pytest.fixture
def support(monkeypatch):
    stubs = {
        "homeassistant.auth.const": {"GROUP_ID_ADMIN": "admin"},
        "homeassistant.components.http": {"HomeAssistantView": object},
        "homeassistant.const": {"EVENT_HOMEASSISTANT_STOP": "homeassistant_stop"},
        "homeassistant.helpers.storage": {"Store": AccountStore},
    }
    for name, values in stubs.items():
        module = ModuleType(name)
        vars(module).update(values)
        monkeypatch.setitem(sys.modules, name, module)
    spec = spec_from_file_location("support_admin_under_test", ROOT / "custom_components/power_sync/support/admin_access.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def configured(support):
    hass = SimpleNamespace(auth=Auth(), loop=asyncio.get_running_loop(), async_create_task=asyncio.create_task)
    manager = support.AdminAccess(hass)
    await manager.initialize()
    return manager, hass


@pytest.mark.asyncio
async def test_owner_approval_is_single_use_and_session_specific(support):
    manager, hass = await configured(support)
    with pytest.raises(support.AccessDenied):
        await manager.approve(ADMIN, CHALLENGE)
    with pytest.raises(support.AccessDenied):
        await manager.redeem(VERIFIER)
    await manager.approve(OWNER, CHALLENGE)
    await manager.revoke(OWNER)
    with pytest.raises(support.AccessDenied):
        await manager.redeem(VERIFIER)
    await manager.approve(OWNER, CHALLENGE)
    with pytest.raises(support.AccessDenied):
        await manager.redeem("b" * 64)
    first, second = await asyncio.gather(manager.redeem(VERIFIER), manager.redeem(VERIFIER), return_exceptions=True)
    assert isinstance(second, support.AccessDenied)
    user = hass.auth.tokens[first["access_token"]].user
    assert user.groups == ["admin"]
    assert manager.status()["state"] == "active"
    with pytest.raises(support.AccessDenied):
        await manager.revoke(ADMIN)
    await manager.revoke(user)
    assert manager.status() == {"state": "closed"}
    assert hass.auth.users == hass.auth.tokens == {}


@pytest.mark.asyncio
async def test_expiry_and_next_boot_remove_granted_account(support):
    manager, hass = await configured(support)
    await manager.approve(OWNER, CHALLENGE)
    await manager._expire_locked(manager.deadline)
    with pytest.raises(support.AccessDenied):
        await manager.redeem(VERIFIER)
    await manager.approve(OWNER, CHALLENGE)
    await manager.redeem(VERIFIER)
    await manager._expire_locked(manager.deadline)
    assert not hass.auth.users and not hass.auth.tokens
    await manager.approve(OWNER, CHALLENGE)
    await manager.redeem(VERIFIER)
    manager.timer.cancel()  # Simulate a process loss before orderly shutdown.
    successor = support.AdminAccess(hass)
    successor.store = manager.store
    assert set(successor.store.value) == {"name"}
    await successor.initialize()
    assert successor.status() == {"state": "closed"}
    assert not hass.auth.users and not hass.auth.tokens


@pytest.mark.asyncio
async def test_token_creation_failure_rolls_back_admin_account(support, monkeypatch):
    manager, hass = await configured(support)
    await manager.approve(OWNER, CHALLENGE)

    def fail(_refresh):
        raise RuntimeError("native token issuance failed")

    monkeypatch.setattr(hass.auth, "async_create_access_token", fail)
    with pytest.raises(RuntimeError):
        await manager.redeem(VERIFIER)
    assert not hass.auth.users and not hass.auth.tokens
    assert manager.status() == {"state": "closed"}
    assert not manager.store.value


def tls_certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(minutes=10))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx, certfile


@pytest.mark.asyncio
async def test_real_stdio_mcp_signs_in_reads_writes_and_revokes_over_tls(support, tmp_path):
    manager, hass = await configured(support)
    settings = {"mode": "initial"}
    renewed = asyncio.Event()

    def account(request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return hass.auth.tokens.get(token)

    async def change(request):
        token = account(request)
        if not token:
            raise web.HTTPUnauthorized
        assert token.user.groups == ["admin"]
        if request.method == "POST":
            settings.update(await request.json())
        return web.json_response(settings)

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        assert auth["access_token"] in hass.auth.tokens
        await ws.send_json({"type": "auth_ok"})
        command = await ws.receive_json()
        assert command["type"] == "test/update"
        settings.update(command["data"])
        await ws.send_json({"id": command["id"], "type": "result", "success": True, "result": settings})
        await ws.close()
        return ws

    async def admin(request):
        token = account(request)
        if not token:
            raise web.HTTPUnauthorized
        request["hass_user"] = token.user
        response = await support.AdminAccessView(manager).post(request)
        if response.status == 200 and "access_token" in json.loads(response.text):
            renewed.set()
        return response

    start = manager.redeem

    async def short_initial_lease(verifier):
        await start(verifier)
        manager._schedule(8)  # Bring the first renewal into the test window.
        return manager._lease_status()

    manager.redeem = short_initial_lease

    async def redirect(_request):
        raise web.HTTPTemporaryRedirect("/api/redirected")

    app = web.Application()
    app.router.add_post(support.PATH + "/redeem", support.AdminRedeemView(manager).post)
    app.router.add_post(support.PATH, admin)
    app.router.add_route("*", "/api/config", change)
    app.router.add_get("/api/redirect", redirect)
    app.router.add_get("/api/websocket", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_context, cert = tls_certificate(tmp_path)
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    host, port = runner.addresses[0]
    assert host == "127.0.0.1"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "scripts/ha_admin_support_mcp.py"), "--origin", f"https://localhost:{port}",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "SSL_CERT_FILE": str(cert)},
    )

    async def rpc(method, params=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        proc.stdin.write(json.dumps(payload).encode() + b"\n")
        await proc.stdin.drain()
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))["result"]

    async def tool(name, arguments=None, *, error=False):
        reply = await rpc("tools/call", {"name": name, "arguments": arguments or {}})
        assert reply["isError"] is error, reply
        text = reply["content"][0]["text"]
        assert support.USER_PREFIX not in text, "native credential leaked into the MCP transcript"
        return text if error else json.loads(text)

    try:
        names = {tool["name"] for tool in (await rpc("tools/list"))["tools"]}
        assert {"support_admin_request", "support_admin_websocket", "support_admin_logout"} <= names
        get_config = {"method": "GET", "path": "/api/config"}
        await tool("support_admin_request", get_config, error=True)
        challenge = (await tool("support_admin_prepare"))["challenge"]
        await tool("support_admin_connect", error=True)
        await manager.approve(OWNER, challenge)
        session = await tool("support_admin_connect")
        assert session["connected"]
        await asyncio.wait_for(renewed.wait(), 5)
        assert manager.expires_at > session["expires_at"] + 1700
        assert (await tool("support_admin_request", get_config))["data"] == {"mode": "initial"}
        update = {"method": "POST", "path": "/api/config", "body": {"mode": "rest"}}
        assert (await tool("support_admin_request", update))["data"] == {"mode": "rest"}
        command = {"type": "test/update", "data": {"mode": "websocket"}}
        assert (await tool("support_admin_websocket", {"command": command}))["success"]
        assert (await tool("support_admin_request", get_config))["data"] == {"mode": "websocket"}
        await tool("support_admin_request", {"method": "GET", "path": "https://other.invalid/api/config"}, error=True)
        await tool("support_admin_request", {"method": "GET", "path": "/api/redirect"}, error=True)
        await tool("support_admin_request", {"method": "POST", "path": support.PATH, "body": {"action": "renew"}}, error=True)
        assert await tool("support_admin_logout") == {"closed": True}
        assert not hass.auth.users and not hass.auth.tokens
        await tool("support_admin_request", get_config, error=True)
        challenge = (await tool("support_admin_prepare"))["challenge"]
        await manager.approve(OWNER, challenge)
        await tool("support_admin_connect")
        proc.stdin.close()
        await asyncio.wait_for(proc.communicate(), 5)
        assert not hass.auth.users and not hass.auth.tokens
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
        await manager.stop(None)
        await runner.cleanup()
