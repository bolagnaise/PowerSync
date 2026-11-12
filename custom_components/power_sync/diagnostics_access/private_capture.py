"""Fresh reserved capture, job-bound encryption, discard on backpressure."""
import asyncio
import secrets
import time
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import wire


def aad(root, grant, job, pair):
    return wire.json_bytes({"purpose": "powersync-private-replay-v1", "root": root, "grant": grant, "job": job, "pair": pair})


def encrypt(raw, public_key, associated):
    private = X25519PrivateKey.generate()
    shared = private.exchange(X25519PublicKey.from_public_bytes(wire.unb64(public_key, size=32)))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=associated).derive(shared)
    nonce = secrets.token_bytes(12)
    return wire.b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) + nonce + AESGCM(key).encrypt(nonce, raw, associated))


class FreshCapture:
    def __init__(self, runtime):
        self.runtime = runtime
        self.active = False

    async def run(self, grant, job):
        if self.active:
            return
        self.active = True
        recorder = coordinator = optimizer = temporal = plant = None
        try:
            from ..optimization import battery_optimizer
            from ..regression.environment import binding
            from ..regression.capture import CaptureRecorder
            from ..regression.temporal_capture import TemporalCapture
            from ..regression.plant_capture import PlantCapture
            from ..regression.contract import canonical
            from ..currency import currency_for_entry
            root = Path(__file__).resolve().parents[1]
            source = await asyncio.to_thread(binding, root, battery_optimizer)
            if source != job.get("source"):
                raise wire.WireError("runtime_pair_unavailable")
            candidates = []
            for entry_id, data in self.runtime.hass.data.get("power_sync", {}).items():
                if type(data) is dict and data.get("optimization_coordinator"):
                    candidates.append((entry_id, data["optimization_coordinator"]))
            if len(candidates) != 1:
                raise wire.WireError("capture_installation_ambiguous")
            entry_id, coordinator = candidates[0]
            if getattr(coordinator, "_regression_recorder", None) is not None:
                raise wire.WireError("capture_busy")
            entry = self.runtime.hass.config_entries.async_get_entry(entry_id)
            recorder = CaptureRecorder(source, currency=currency_for_entry(entry, self.runtime.hass), max_cycles=1)
            await self.runtime.command(grant, {"type": "job_capture", "id": job["id"]}, "boot")
            profiles=job.get("profiles",["solver_v1"])
            if "reserve-recovery-temporal-v1" in profiles:
                temporal = TemporalCapture()
                coordinator._regression_temporal_recorder = temporal
            if "calibrated-plant-v1" in profiles:
                plant=PlantCapture(self.runtime.hass,entry_id)
            coordinator._regression_recorder = recorder
            until = min(job["deadline"] - 300, time.time() + (900 if plant and plant.available else 90))
            next_sample=time.time()
            while time.time() < until and (not recorder.cycles or plant and plant.available):
                if plant and time.time()>=next_sample:
                    plant.sample();next_sample=time.time()+30
                if self.runtime.closed or self.runtime.root_pending or grant.get("revoking") or not grant.get("active"):
                    raise wire.WireError("capture_cancelled")
                await asyncio.sleep(.25)
            if not recorder.cycles:
                raise wire.WireError("fresh_capture_unavailable")
            if temporal:
                await asyncio.sleep(5)
            raw = await asyncio.to_thread(lambda: canonical({**recorder.export(), **({"temporal":temporal.export()} if temporal else {}), **({"plant":plant.export()} if plant else {})}))
            prepared_bytes=len(raw)
            encrypted = await asyncio.to_thread(encrypt, raw, job["encryption_key"],
                aad(self.runtime.identity["root"], grant["id"], job["id"], job["pair"]))
            del raw
            chunks = [encrypted[index:index+160000] for index in range(0, len(encrypted), 160000)]
            for index, chunk in enumerate(chunks):
                await self.runtime.command(grant, {"type": "job_chunk", "id": job["id"], "index": index,
                    "chunk": chunk, "final": index == len(chunks)-1}, "boot")
            grant['activity']={'last_capture_sent_at':int(time.time()),'capture_bytes':prepared_bytes,
                'capture_ms':round(recorder.capture_seconds*1000),'dropped_cycles':recorder.dropped}
            await self.runtime.save()
        finally:
            if coordinator is not None and getattr(coordinator, "_regression_recorder", None) is recorder:
                coordinator._regression_recorder = None
            optimizer = getattr(coordinator, "_optimizer", None)
            if optimizer is not None and getattr(optimizer, "_regression_recorder", None) is recorder:
                optimizer._regression_recorder = None
            if plant is not None:
                plant.close()
            if temporal is not None:
                temporal.close()
                if getattr(coordinator, "_regression_temporal_recorder", None) is temporal:
                    coordinator._regression_temporal_recorder = None
            if recorder is not None:
                recorder._drop(detach=True)
                recorder.cycles.clear()
            self.active = False
