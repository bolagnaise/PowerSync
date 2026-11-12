"""One reserved private job; public keys and immutable artifact IDs only in metadata."""
import argparse
from contextlib import contextmanager
import fcntl
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import stat
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components/power_sync"))
from diagnostics_access import wire
from diagnostics_access.bridge import Bridge, private_file
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from private_regression.sandbox import no_swap, purge, reap, docker, Sandbox, LABEL
from regression_runner.runtime import component_digest
from private_regression.images import verify_image
from private_regression.fingerprints import scorer_digest,schema_digest


@contextmanager
def runner_slot():
    directory=Path('/tmp')/('powersync-private-runner-'+str(os.getuid()))
    directory.mkdir(mode=0o700,exist_ok=True)
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('runner_lock_unavailable')
    descriptor=os.open(directory/'admission',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def run(config,pair,transport=None):
    with runner_slot():
        client=Bridge(config,config['profile'],transport)
        try:
            return execute(config,pair,transport,client=client)
        finally:
            client.close()


def recover(config,job,transport=None):
    """Metadata-only recovery after a real host cleanup, never a replay retry."""
    with runner_slot():
        no_swap()
        confirmed=purge(job)
        client=Bridge(config,config['profile'],transport)
        request=wire.packet(config['runner_private_key'],'runner',config['root'],config['grant'],
            {'type':'job_purged','id':job,'runner':config['runner'],'runner_generation':config['runner_generation'],'confirmed':confirmed})
        response=client.transport('/command',request)
        payload=wire.verify(response,config['authority_key'],'authority',config['root'],config['grant'])
        if payload.get('request_nonce')!=request['metadata']['nonce'] or payload.get('error'):
            raise ValueError('cleanup_receipt_unconfirmed')
        return {'job':job,'purge':'confirmed' if confirmed else 'unconfirmed'}


def execute(config, pair, transport=None, *, client=None):
    no_swap(); reap()
    if pair.get('scorer')!=scorer_digest() or pair.get('schema')!=schema_digest():
        raise ValueError('trusted_scorer_or_schema_changed')
    if docker('ps','-aq','--filter','label='+LABEL).strip():
        raise ValueError('private_cleanup_unconfirmed')
    verified=set()
    for arm in ("baseline", "candidate"):
        artifact = pair[arm]
        if component_digest(Path(artifact["root"])) != artifact["component"]:
            raise ValueError("artifact_changed")
        image = json.loads(docker("image", "inspect", artifact["image"]))[0]
        if image["Id"] != artifact["image"] or image["Config"]["Labels"].get("cc.powersync.component") != artifact["component"] or image["Config"]["Labels"].get("cc.powersync.runtime") != pair["runtime"]:
            raise ValueError("unverified_runner_artifact")
        if artifact['image'] not in verified:
            verify_image(artifact['image'],artifact['component'])
            probe=Sandbox(secrets.token_hex(32),time.time()+30)
            try:
                health=probe.run(artifact['image'],artifact['component'],None,mode='health')
                if health!={'source':{**pair['source'],'component_sha256':artifact['component']}}:
                    raise ValueError('runner_runtime_incompatible')
            finally:
                if not probe.close():
                    raise ValueError('probe_cleanup_unconfirmed')
            verified.add(artifact['image'])
    profile = config["profile"]
    client = client or Bridge(config, profile, transport)
    descriptor = client.command({"type": "describe"})
    if descriptor["kind"] != "optimizer_contribution":
        raise ValueError("contribution_grant_required")
    binding = descriptor["binding"]
    family = secrets.token_hex(32)
    client.family=family;client.binding=binding
    client.command({"type": "delegate", "family": family, "binding": binding, "refresh_hash": wire.secret_hash(secrets.token_urlsafe(32))})
    job = secrets.token_hex(32)
    digest = hashlib.sha256(wire.json_bytes(pair)).hexdigest()
    result = client.command({"type": "job_reserve", "family": family, "binding": binding, "id": job,
        "pair": digest, "runner": config["runner"], "runner_generation": config["runner_generation"], "source": pair["source"], "profiles":pair.get("required_profiles",["solver_v1"])})
    private = X25519PrivateKey.generate()
    def runner(command):
        request = wire.packet(config["runner_private_key"], "runner", config["root"], config["grant"],
            {**command, "id": job, "runner": config["runner"], "runner_generation": config["runner_generation"]})
        response = client.transport("/command", request)
        payload = wire.verify(response, config["authority_key"], "authority", config["root"], config["grant"])
        if payload.get("request_nonce") != request["metadata"]["nonce"] or payload.get("error"):
            raise ValueError("runner_denied")
        return payload["value"]
    runner({"type": "job_ready", "encryption_key": wire.b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))})
    summary, process = {"status": "invalid", "attempts": 0, "violations": 0, "coverage": "insufficient", "cost_delta": 0}, None
    try:
        chunks = []
        # No capture backlog: this single reservation owns one ephemeral stream.
        capture_until = min(result["deadline"]-15, time.time()+(1020 if "calibrated-plant-v1" in pair.get("required_profiles",[]) else 120))
        while time.time() < capture_until:
            status = client.command({"type": "job_status", "id": job, "family": family, "binding": binding})
            if status["state"] == "running":
                while True:
                    if time.time() >= result['deadline']-15:
                        raise ValueError('stream_expired')
                    part = runner({"type": "job_take"})
                    chunks.append(part["chunk"])
                    if sum(map(len, chunks)) > 6*1024*1024:
                        raise ValueError("stream_limit")
                    if part["final"]:
                        break
                break
            if status["state"] in ("cancelled", "purge_unconfirmed"):
                raise ValueError("job_cancelled")
            time.sleep(.5)
        if not chunks:
            raise ValueError("fresh_capture_timeout")
        request = {"encrypted": "".join(chunks), "decryption_key": wire.b64(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())),
            "pair": pair, "deadline": result["deadline"]-5, "context": {"root": config["root"], "grant": config["grant"], "job": job, "pair": digest}}
        del chunks, private
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("job.py"))], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        encoded = json.dumps(request).encode()
        del request
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(process.communicate, encoded, timeout=max(.1,result['deadline']-time.time()-5))
            try:
                while not pending.done():
                    # Fresh authority/boot checks also fence running sandboxes.
                    # Revocation or a lost cloud lease kills the private process;
                    # its pidfd watchdog independently purges its containers.
                    state = client.command({'type':'job_status','id':job,'family':family,'binding':binding})
                    if state['state'] in ('cancelled','purge_unconfirmed'):
                        raise ValueError('job_cancelled')
                    time.sleep(1)
                output, _ = pending.result()
            finally:
                if process.poll() is None:
                    process.kill();process.wait()
        if process.returncode or len(output) > 4096:
            raise ValueError("private_job_failed")
        summary = json.loads(output)
    finally:
        if process and process.poll() is None:
            process.kill(); process.wait()
        try:
            confirmed = purge(job)
        except Exception:
            confirmed = False
        runner({"type": "job_purged", "confirmed": confirmed, **({"summary": summary} if confirmed and summary else {})})
    return {"job": job, "pair": digest, "purge": "confirmed" if confirmed else "unconfirmed", "summary": summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--pair")
    parser.add_argument('--recover-job')
    args = parser.parse_args()
    if bool(args.pair)==bool(args.recover_job):
        parser.error('Choose one pair or recovery job')
    config=private_file(args.config)
    result=recover(config,args.recover_job) if args.recover_job else run(config,json.loads(Path(args.pair).read_bytes()))
    print(json.dumps(result, allow_nan=False))
