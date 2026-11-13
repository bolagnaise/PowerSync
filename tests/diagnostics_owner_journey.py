"""Cross-repository real HTTP/workerd journey. Synthetic owners and HA sources."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import urllib.request
from types import SimpleNamespace, ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components/power_sync"))
import aiohttp
package = ModuleType("power_sync")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/power_sync")]
sys.modules["power_sync"] = package
from power_sync.diagnostics_access import wire
from power_sync.diagnostics_access.runtime import AccessRuntime
from diagnostics_access.bridge import Bridge
from ha_read.reader import HAReadBackend
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))


class Store:
    def __init__(self):
        self.value = None
    async def async_load(self):
        return copy.deepcopy(self.value)
    async def async_save(self, value):
        self.value = copy.deepcopy(value)


class Session:
    def __init__(self, session):
        self.session = session
    def post(self, url, **kwargs):
        assert url.startswith(wire.ORIGIN + "/")
        return self.session.post(os.environ["DIAGNOSTICS_TEST_URL"] + url.removeprefix(wire.ORIGIN), **kwargs)


def http(route, value):
    request = urllib.request.Request(os.environ["DIAGNOSTICS_TEST_URL"] + route, data=wire.json_bytes(value), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return wire.loads(response.read(420 * 1024 + 1), maximum=420 * 1024)


async def main():
    store = Store()
    states=[]
    backend = HAReadBackend(SimpleNamespace(states=SimpleNamespace(async_all=lambda: list(states))))
    async with aiohttp.ClientSession() as raw:
        session = Session(raw)
        runtime = AccessRuntime(None, store, session, backend)
        await runtime.load()
        assert runtime.task is None and runtime.identity is None
        identity = await runtime.setup_identity()
        clients = []
        for profile in ("codex", "claude"):
            private, public = wire.keypair()
            invitation = await asyncio.to_thread(http, "/invite", {"root": identity["root"], "profile": profile, "public_key": public, "kind": "ha_read"})
            review = await runtime.review("ha-administrator", invitation["code"], os.environ["DIAGNOSTICS_AUTHORITY_PUBLIC"])
            assert review["descriptor"]["registration"]["profile"] == profile
            assert invitation["code"].split(".")[1] not in runtime.grants
            try:
                await runtime.confirm("different-administrator", review["challenge"])
            except wire.WireError:
                pass
            else:
                raise AssertionError("cross-user confirmation")
            review = await runtime.review("ha-administrator", invitation["code"], os.environ["DIAGNOSTICS_AUTHORITY_PUBLIC"])
            await runtime.confirm("ha-administrator", review["challenge"])
            grant = invitation["code"].split(".")[1]
            clients.append(Bridge({"profile": profile, "root": identity["root"], "grant": grant, "private_key": private, "authority_key": os.environ["DIAGNOSTICS_AUTHORITY_PUBLIC"]}, profile, http))
            for _ in range(100):
                if runtime.grants[grant].get("active"):
                    break
                await asyncio.sleep(.1)
            assert runtime.grants[grant].get("active"), runtime.last_error
            result = await asyncio.to_thread(clients[-1].dispatch, "tools/call", {"name": "ha_get_capabilities", "arguments": {}})
            assert "structuredContent" in result
        from datetime import datetime,timezone
        clock=datetime.now(timezone.utc)
        states.append(SimpleNamespace(entity_id='sensor.fidelity',state='1.0',attributes={'integral_float':1.0,'tiny':1e-7,'unicode':'☀️'},last_changed=clock,last_updated=clock))
        values=(await asyncio.to_thread(clients[0].call,'ha_get_states',{}))['result']['records'][0]['attributes']
        assert type(values['integral_float']) is float and values['integral_float']==1.0 and values['tiny']==1e-7 and values['unicode']=='☀️'
        states.clear()
        if os.environ.get("DIAGNOSTICS_NATIVE_CLIENTS"):
            from diagnostics_native_clients import native
            await native(clients)
        # Persistent consent survives a process boot, never its active boot key.
        await runtime.stop()
        runtime = AccessRuntime(None, store, session, backend)
        await runtime.load()
        assert all(not x["active"] for x in runtime.grants.values())
        for _ in range(100):
            if all(x.get("active") for x in runtime.grants.values()):
                break
            await asyncio.sleep(.1)
        result = await asyncio.to_thread(clients[1].call, "ha_get_states", {})
        assert result["result"]["records"] == []
        if os.environ.get("DIAGNOSTICS_PRIVATE_ARTIFACT"):
            await contribution(runtime, identity)
        await runtime.revoke(clients[0].config["grant"])
        for _ in range(100):
            if clients[0].config["grant"] not in runtime.grants:
                break
            await asyncio.sleep(.1)
        try:
            await asyncio.to_thread(clients[0].call, "ha_get_states", {})
        except Exception:
            pass
        else:
            raise AssertionError("revoked Codex read survived")
        await asyncio.to_thread(clients[1].call, "ha_get_states", {})
        await runtime.revoke()
        for _ in range(100):
            if runtime.identity is None:
                break
            await asyncio.sleep(.1)
        assert runtime.identity is None, runtime.last_error
        try:
            await asyncio.to_thread(clients[1].call, "ha_get_states", {})
        except Exception:
            pass
        else:
            raise AssertionError("root-revoked Claude read survived")
        await runtime.stop()
    print("owner journey passed")


async def contribution(runtime, identity):
    from datetime import datetime, timezone, timedelta
    from regression_runner.runtime import load_component
    from private_regression.controller import run
    from private_regression.sandbox import daemon_identity
    artifact = Path(os.environ["DIAGNOSTICS_PRIVATE_ARTIFACT"])
    metadata = json.loads((artifact/"artifact.json").read_bytes())
    root = artifact/"custom_components/power_sync"
    module = load_component(root, datetime(2026, 11, 7, tzinfo=timezone.utc))
    from power_sync.regression.environment import binding
    source = await asyncio.to_thread(binding, root, module)
    currency = ModuleType("power_sync.currency")
    currency.currency_for_entry = lambda *_: "AUD"
    sys.modules[currency.__name__] = currency
    coordinator = SimpleNamespace(_optimizer=module.BatteryOptimizer(interval_minutes=60, horizon_hours=8))
    runtime.hass = SimpleNamespace(data={"power_sync": {"entry": {"optimization_coordinator": coordinator}}},
                                   config_entries=SimpleNamespace(async_get_entry=lambda _: object()))
    private, public = wire.keypair()
    invitation = await asyncio.to_thread(http, "/invite", {"root": identity["root"], "profile": "codex", "public_key": public, "kind": "optimizer_contribution"})
    review = await runtime.review("ha-administrator", invitation["code"], os.environ["DIAGNOSTICS_AUTHORITY_PUBLIC"])
    await runtime.confirm("ha-administrator", review["challenge"])
    grant_id = invitation["code"].split(".")[1]
    for _ in range(100):
        if runtime.grants[grant_id].get("active"):
            break
        await asyncio.sleep(.1)
    config = {"profile": "codex", "private_key": private, "root": identity["root"], "grant": grant_id,
              "authority_key": os.environ["DIAGNOSTICS_AUTHORITY_PUBLIC"], "runner": "c"*32,
              "runner_generation": "d"*32, "daemon_id": await asyncio.to_thread(daemon_identity), "pid_namespace": os.readlink("/proc/self/ns/pid"), "runner_private_key": os.environ["DIAGNOSTICS_RUNNER_SEED"]}
    pair = {"baseline": {"root": str(root), "image": metadata["image"], "component": metadata["component"]},
            "candidate": {"root": str(root), "image": metadata["image"], "component": metadata["component"]},
            "source": source, "runtime": metadata["runtime"],"scorer":metadata['scorer'],"schema":metadata['schema'],'currency':'AUD'}
    controller = asyncio.create_task(asyncio.to_thread(run, config, pair, http))
    for _ in range(200):
        if getattr(coordinator, "_regression_recorder", None):
            break
        if controller.done():
            await controller
        await asyncio.sleep(.1)
    recorder = coordinator._regression_recorder
    recorder.start_cycle()
    coordinator._optimizer._regression_recorder = recorder
    clock = datetime(2026, 11, 7, tzinfo=timezone.utc)
    result = await asyncio.to_thread(coordinator._optimizer.optimize, import_prices=[.05]*4+[.45]*4,
        export_prices=[.02]*8, solar_forecast=[0.0]*8, load_forecast=[.5]*8, current_soc=.5,
        schedule_timestamps=[clock+timedelta(hours=index) for index in range(8)])
    recorder.record_stage("committed", result.schedule)
    recorder.finish_cycle("committed")
    report = await asyncio.wait_for(controller, timeout=150)
    assert report["purge"] == "confirmed" and report["summary"]["status"] == "pass", report
    assert not recorder.cycles


if __name__ == "__main__":
    asyncio.run(main())
