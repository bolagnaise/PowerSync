#!/usr/bin/env python3
"""Generate a local PowerSync recovery response from a downloaded HA backup."""
import argparse
import base64
from datetime import datetime
import hmac
import json
import re
import sys
import tarfile

PURPOSE = b"PowerSync ESPHome Stop continuity v1\0"
MAX_STORE_BYTES = 32 * 1024 * 1024
STORE_FILES = ("core.config_entries", "core.entity_registry")


class ProofError(ValueError):
    """An owner-actionable error without backup contents."""


def _json_member(archive, member):
    if not member.isfile() or not 0 < member.size <= MAX_STORE_BYTES:
        raise ProofError("A required backup file is missing or too large.")
    with archive.extractfile(member) as contents:
        return json.load(contents)


def _read_backup(path):
    stores = {}
    with tarfile.open(path, "r:*") as backup:
        members = {x.name.removeprefix("./"): x for x in backup}
        metadata = _json_member(backup, members["backup.json"])
        homeassistant = members["homeassistant.tar.gz"]
        if not homeassistant.isfile():
            raise ProofError("The archive does not contain Home Assistant settings.")
        with backup.extractfile(homeassistant) as contents, tarfile.open(fileobj=contents, mode="r|*") as core:
            for item in core:
                name = item.name.removeprefix("./").removeprefix("data/")
                if name.startswith(".storage/") and name[len(".storage/"):] in STORE_FILES:
                    store_name = name[len(".storage/"):]
                    if store_name in stores:
                        raise ProofError("The backup contains duplicate registry files.")
                    stores[store_name] = _json_member(core, item)
                    if len(stores) == len(STORE_FILES):
                        break
    return metadata, stores


def _key(entry):
    try:
        encoded = entry.get("data", {}).get("noise_psk")
        key = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
    except ValueError:
        return None
    return key if key is not None and len(key) == 32 else None


def _mac(value):
    return re.sub(r"[:-]", "", str(value or "")).lower()


def generate_response(path, challenge):
    if not isinstance(challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{100,2048}={0,2}", challenge):
        raise ProofError("Paste the PowerSync challenge from the prepare step.")
    try:
        scope = json.loads(base64.b64decode(challenge, altchars=b"-_", validate=True))
        if scope["version"] != 1:
            raise ProofError("The challenge uses an unsupported version.")
        original = scope["original"]
        readded_at = datetime.fromisoformat(scope["current_created_at"])
        metadata, stores = _read_backup(path)
        backed_up_at = datetime.fromisoformat(metadata["date"].replace("Z", "+00:00"))
        if backed_up_at.tzinfo is None or readded_at.tzinfo is None or backed_up_at >= readded_at:
            raise ProofError("Select a Home Assistant backup from before the bridge was re-added.")
        entries = [x for x in stores["core.config_entries"]["data"]["entries"] if x["domain"] == "esphome"]
        historical = [x for x in entries if x["entry_id"] == original["config_entry_id"]]
        if (len(historical) != 1 or _mac(historical[0]["unique_id"]) != scope["mac"]
                or any(x["entry_id"] == scope["current_entry_id"] for x in entries)):
            raise ProofError("The backup does not contain the original ESPHome integration entry.")
        if sum(_mac(x.get("unique_id")) == scope["mac"] for x in entries) != 1:
            raise ProofError("The original ESPHome device identity is ambiguous in this backup.")
        entities = stores["core.entity_registry"]["data"]["entities"]
        charger = [x for x in entities if x["entity_id"] == original["entity_id"]]
        if len(charger) != 1 or any(charger[0].get(key) != original[key] for key in (
            "platform", "config_entry_id", "device_id", "unique_id", "disabled_by",
        )):
            raise ProofError("The original charger binding is missing or differs in this backup.")
        key = _key(historical[0])
        if key is None or sum(_key(x) == key for x in entries) != 1:
            raise ProofError("The original ESPHome key is missing or shared with another bridge in this backup.")
    except ProofError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, tarfile.TarError):
        raise ProofError("Use the unencrypted download of an older Home Assistant backup containing its settings.") from None
    return hmac.digest(key, PURPOSE + challenge.encode("ascii"), "sha256").hex()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", help="Path to the locally downloaded Home Assistant backup")
    parser.add_argument("challenge", help="Challenge shown in Home Assistant")
    arguments = parser.parse_args()
    try:
        print(generate_response(arguments.backup, arguments.challenge))
    except ProofError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
