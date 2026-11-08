"""Boot-bound local consent and signed disclosure; contains no transport or HA."""
from __future__ import annotations

import secrets
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .protocol import CATEGORIES, SupportError, b64, digest, encode, identifier, number, query, validate_evidence


class Session:
    def __init__(self, *, wall=time.time, mono=time.monotonic):
        self.wall, self.mono = wall, mono
        self.boot = uuid.uuid4().hex
        self.generation = 0
        self.state = "closed"
        self.key = self.token = None
        self.owner = None
        self.scope = {}
        self.requests = {}
        self.retired = set()
        self.uploaded = 0
        self.audit = []

    def sign(self, value):
        if self.key is None:
            raise SupportError("session_closed")
        raw = encode(value)
        return {"signed": b64(raw), "signature": b64(self.key.sign(raw))}

    def begin(self, owner, entry_ref, categories, history_start, duration, *, include_recorder=False):
        self.close("replaced")
        if not isinstance(categories, list) or not categories or set(categories) - CATEGORIES or len(categories) != len(set(categories)):
            raise SupportError("invalid_categories")
        if type(duration) is not int or not 60 <= duration <= 28800:
            raise SupportError("invalid_duration")
        now = self.wall()
        if number(history_start) is None or not now - 86400 <= history_start <= now:
            raise SupportError("invalid_history_range")
        self.key = Ed25519PrivateKey.generate()
        self.owner = owner
        self.id = uuid.uuid4().hex
        self.nonce = secrets.token_urlsafe(24)
        self.scope = {"schema_version": 1, "session_id": self.id, "entry_ref": entry_ref, "boot": self.boot,
            "generation": self.generation, "categories": sorted(categories), "history_start": history_start,
            "expires_at": now + duration, "ha_recorder": include_recorder}
        self.scope_digest = digest(self.scope)
        self.deadline = self.mono() + duration
        self.state = "pending"
        self.pending_deadline = self.mono() + 300
        self.requests = {}
        self.retired = set()
        self.uploaded = 0
        self.audit = []
        return {"scope": self.scope, "scope_digest": self.scope_digest,
            "public_key": b64(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}

    def check(self, *, active=True):
        if self.key is None:
            raise SupportError("session_closed")
        if self.wall() >= self.scope["expires_at"] or self.mono() >= self.deadline:
            self.close("expired")
            raise SupportError("session_expired")
        if self.state in {"pending", "activating"} and self.mono() >= self.pending_deadline:
            self.close("pairing_expired")
            raise SupportError("pairing_expired")
        if active and self.state != "active":
            raise SupportError("session_inactive")

    def confirm(self, owner, descriptor, *, policy_digest):
        self.check(active=False)
        if owner != self.owner or self.state not in {"pending", "activating"} or descriptor.get("scope_digest") != self.scope_digest or descriptor.get("policy_digest") != policy_digest:
            raise SupportError("confirmation_mismatch")
        for name in ("actor", "ticket", "reporter", "phrase"):
            if not isinstance(descriptor.get(name), str) or not 1 <= len(descriptor[name]) <= 200:
                raise SupportError("invalid_invitation")
        if self.state == "activating" and descriptor != self.descriptor:
            raise SupportError("confirmation_mismatch")
        self.descriptor = dict(descriptor)
        self.state = "activating"
        return self.sign({"kind": "activate", "session_id": self.id, "scope_digest": self.scope_digest,
            "descriptor_digest": digest(descriptor)})

    def activated(self):
        self.check(active=False)
        if self.state != "activating":
            raise SupportError("session_inactive")
        self.state = "active"

    def admit(self, job):
        self.check()
        if not isinstance(job, dict) or set(job) != {"request_id", "scope_digest", "expires_at", "query"}:
            raise SupportError("invalid_request")
        if not identifier(job["request_id"]) or job["scope_digest"] != self.scope_digest:
            raise SupportError("request_scope_denied")
        now = self.wall()
        if number(job["expires_at"]) is None or not now < job["expires_at"] <= min(now + 36, self.scope["expires_at"]):
            raise SupportError("request_expired")
        q = query(job["query"], now=now, start=self.scope["history_start"], end=self.scope["expires_at"])
        if q["category"] not in self.scope["categories"] or (q.get("source") == "ha_recorder" and not self.scope["ha_recorder"]):
            raise SupportError("category_denied")
        for request_id, receipt in list(self.requests.items()):
            if receipt.get("sealed_at", receipt["expires_at"]) + 65 < now:
                self.retired.add(request_id)
                del self.requests[request_id]
        if job["request_id"] in self.retired:
            raise SupportError("request_retired")
        fingerprint = digest(job)
        previous = self.requests.get(job["request_id"])
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise SupportError("request_id_reused")
            return q, previous.get("packet")
        if len(self.requests) >= 128 or len(self.retired) >= 8192:
            raise SupportError("request_limit")
        self.requests[job["request_id"]] = {"fingerprint": fingerprint, "expires_at": job["expires_at"]}
        return q, None

    def seal(self, job, result):
        self.check()
        validate_evidence(result)
        receipt = self.requests.get(job["request_id"])
        if receipt is None or receipt["fingerprint"] != digest(job) or self.wall() >= receipt["expires_at"]:
            raise SupportError("request_expired")
        packet = self.sign({"kind": "result", "session_id": self.id, "scope_digest": self.scope_digest,
            "request_id": job["request_id"], "result": result, "result_digest": digest(result), "captured_at": self.wall()})
        self.uploaded += len(encode(packet))
        if self.uploaded > 16 * 1024 * 1024:
            self.close("byte_limit")
            raise SupportError("byte_limit")
        for previous in self.requests.values():
            if previous.get("sealed_at", self.wall()) + 60 < self.wall():
                previous.pop("packet", None)
                previous.pop("result_digest", None)
        receipt["sealed_at"] = self.wall()
        receipt["packet"] = packet
        receipt["result_digest"] = digest(result)
        self.audit.append({"at": self.wall(), "category": job["query"]["category"], "request_id": job["request_id"]})
        self.audit = self.audit[-128:]
        return packet

    def release(self, challenge):
        self.check()
        if set(challenge) != {"request_id", "result_digest", "nonce", "expires_at"} or not identifier(challenge["nonce"]):
            raise SupportError("invalid_release")
        receipt = self.requests.get(challenge["request_id"])
        if not receipt or receipt.get("result_digest") != challenge["result_digest"] or not self.wall() - 5 < challenge["expires_at"] <= self.wall() + 11:
            raise SupportError("release_denied")
        return self.sign({"kind": "release", "session_id": self.id, "scope_digest": self.scope_digest, **challenge})

    def close(self, reason="revoked"):
        self.generation += 1
        self.state = "closed"
        self.reason = reason
        self.key = self.token = None
        self.requests = {}

    def status(self):
        if self.key is not None:
            try:
                self.check(active=False)
            except SupportError:
                pass
        return {"state": self.state, "reason": getattr(self, "reason", None), "scope": dict(self.scope),
            "descriptor": dict(getattr(self, "descriptor", {})), "audit": list(self.audit)}
