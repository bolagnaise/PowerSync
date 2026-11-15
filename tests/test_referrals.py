"""Referral display cannot change licensing, leak credentials, or cross a pairing reset."""
import ast
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1] / "custom_components/power_sync"


def referral_method():
    tree = ast.parse((ROOT / "licensing/manager.py").read_text())
    manager = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HALicenseManager")
    method = next(node for node in manager.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_referrals")
    namespace = {"time": SimpleNamespace(time=lambda: 1800000000)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "manager.py", "exec"), namespace)
    return namespace["async_referrals"]


def summary(**changes):
    return {"enabled": True, "code": "PSABCDEFGHJK", "share_url": "https://untrusted.invalid/ignored", "earned_this_year": 2,
            "annual_limit": 12, "year": 2027, "banked_rewards": 1, **changes}


def manager():
    return SimpleNamespace(paired=True, installation_id="installation_one", credential="server_only_credential",
                           _transport=AsyncMock(return_value=summary()), admission=object(), entries={})


def test_referral_fetch_uses_pairing_proof_without_rendering_credentials_or_remote_urls():
    async def run():
        client = manager()
        admission = client.admission
        result = await referral_method()(client)
        assert result["share_url"] == "https://powersync.cc/app/refer/PSABCDEFGHJK"
        assert result["account_url"] == "https://powersync.cc/app/referrals"
        assert "credential" not in str(result)
        assert client.admission is admission and client.entries == {}
        client._transport.assert_awaited_once_with("/referrals", {
            "installation_id": "installation_one", "credential": "server_only_credential"})
        assert await referral_method()(client) == result
        assert client._transport.await_count == 1
    asyncio.run(run())


def test_outage_keeps_same_pairing_cache_and_never_reuses_it_after_pairing_changes():
    async def run():
        client = manager()
        await referral_method()(client)
        client._transport.side_effect = TimeoutError()
        stale = await referral_method()(client, force=True)
        assert stale["stale"] and stale["unavailable"] and stale["code"] == "PSABCDEFGHJK"
        client.credential = "replacement_credential"
        replacement = await referral_method()(client, force=True)
        assert replacement == {"paired": True, "unavailable": True}
        client.paired = False
        assert await referral_method()(client) == {"paired": False, "unavailable": False}
        assert client._referral_cache is None
    asyncio.run(run())


def test_malformed_or_injected_codes_are_not_cached():
    async def run():
        for invalid in [summary(code="<script>"), summary(annual_limit=True), summary(banked_rewards=-1)]:
            client = manager()
            client._transport.return_value = invalid
            assert await referral_method()(client) == {"paired": True, "unavailable": True}
            assert not hasattr(client, "_referral_cache")
    asyncio.run(run())


def test_referral_copy_preserves_access_and_resume_guidance():
    spec = importlib.util.spec_from_file_location("referral_presentation", ROOT / "licensing/presentation.py")
    presentation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(presentation)
    status = {"paired": True, "access_state": "paid", "can_optimize": True,
              "account_url": "https://powersync.cc/app/ha", "referrals": summary()}
    result = presentation.optimizer_access_description(status)
    assert result["access_summary"] == "Active HA optimiser subscription."
    assert "No Resume action" in result["next_step"]
    assert "PSABCDEFGHJK" in result["referral_summary"]
    status.update(can_optimize=False, requires_resume=True, would_allow=True)
    assert "Resume optimisation" in presentation.optimizer_access_description(status)["next_step"]


def test_paused_program_is_visible_without_affecting_existing_access():
    async def run():
        client = manager()
        client._transport.return_value = summary(enabled=False)
        result = await referral_method()(client)
        assert result["enabled"] is False
        assert result["code"] == "PSABCDEFGHJK"
        assert client.entries == {} and client.admission is not None
    asyncio.run(run())
