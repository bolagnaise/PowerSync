"""Real OS-sandbox acceptance with public, generated optimizer inputs only."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"custom_components/power_sync"))
from replay_optimizer import capture_synthetic
from regression_runner import trusted_contract as contract
from regression_runner.runtime import load_component
from diagnostics_access.private_capture import encrypt, aad
from diagnostics_access import wire
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from private_regression.sandbox import purge


def main(artifact):
    metadata = json.loads((artifact/"artifact.json").read_bytes())
    root = artifact/"custom_components/power_sync"
    bundle = capture_synthetic(root, "flat-tariff", "AUD")
    from power_sync.regression.environment import binding
    module = sys.modules["power_sync.optimization.battery_optimizer"]
    bundle["source"] = binding(root, module)
    bundle["digest"] = contract.digest({key:value for key,value in bundle.items() if key != "digest"})
    pair = {"baseline": {"root": str(root), "image": metadata["image"], "component": metadata["component"]},
            "candidate": {"root": str(root), "image": metadata["image"], "component": metadata["component"]},
            "source": bundle["source"], "runtime": metadata["runtime"],"scorer":metadata['scorer'],"schema":metadata['schema']}
    digest = hashlib.sha256(wire.json_bytes(pair)).hexdigest()
    context = {"root": "a"*64, "grant": "b"*32, "job": secrets.token_hex(32), "pair": digest}
    private = X25519PrivateKey.generate()
    encrypted = encrypt(contract.canonical({"cycles": [{"attempts": [bundle]}]}),
        wire.b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)), aad(**context))
    request = {"encrypted": encrypted, "decryption_key": wire.b64(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())),
               "pair": pair, "deadline": time.time()+120, "context": context}
    process = subprocess.run([sys.executable, "-c", 'import runpy,sys;runpy.run_path(sys.argv[1],run_name="synthetic_smoke")["main"]()', str(Path(__file__).with_name("job.py"))], input=json.dumps(request).encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=130)
    assert process.returncode == 0, process.stderr.decode()[-3000:]
    report = json.loads(process.stdout)
    assert report["status"] == "pass", report
    assert purge(context["job"])
    print(json.dumps({"status": "passed", "real_os_sandbox": True, "baseline_candidate": report,
                      "purge": "confirmed", "image": metadata["image"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    main(parser.parse_args().artifact)
