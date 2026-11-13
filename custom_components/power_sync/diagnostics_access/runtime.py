"""Administrator consent and outbound read transport. Default off; no HA writes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import secrets
import time
from contextlib import suppress

from . import wire

DATA = "power_sync_diagnostics_access"


class AccessRuntime:
    def __init__(self, hass, store, session, backend):
        self.hass, self.store, self.session, self.backend = hass, store, session, backend
        self.identity = None
        self.grants = {}
        self.reviews = {}
        self.task = None
        self.closed = False
        self.root_pending = False
        self.root_pin = None
        self.lock = asyncio.Lock()
        self.save_lock = asyncio.Lock()
        self.last_error = None
        self.capture_task = None

    async def load(self):
        value = await self.store.async_load() or {}
        self.identity = value.get("identity")
        self.grants = value.get("grants", {})
        self.root_pending = value.get("root_pending", False)
        self.root_pin = value.get("root_pin")
        # No read continues on an old process boot, even when its lease remains.
        for grant in self.grants.values():
            grant["active"] = False
            grant['resume_wait'] = True
            if not grant.get("boot_pending"):
                grant.pop("boot", None)
        if self.identity and (self.grants or self.root_pending):
            self.start()

    async def save(self):
        async with self.save_lock:
            await self.store.async_save(copy.deepcopy({"identity": self.identity, "grants": self.grants, "root_pending": self.root_pending, "root_pin": self.root_pin}))

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop())

    async def setup_identity(self):
        if self.root_pending:
            raise wire.WireError('revocation_pending')
        if not self.identity:
            private, public = wire.keypair()
            self.identity = {"private": private, "public": public, "root": wire.key_id(public)}
            await self.save()
        return {"root": self.identity["root"], "public_key": self.identity["public"]}

    async def post(self, route, value):
        async with self.session.post(wire.ORIGIN + route, data=wire.json_bytes(value),
                                     headers={"Content-Type": "application/json"},
                                     allow_redirects=False, timeout=15) as response:
            raw = await response.content.read(420 * 1024 + 1)
            result = wire.loads(raw, maximum=420 * 1024)
            if response.status != 200:
                raise wire.WireError("remote_request_denied")
            return result

    async def command(self, grant, command, purpose="installation"):
        cleanup = command["type"] in ("revoke", "cancel_invitation", "owner_status", "close_boot")
        if not cleanup and (self.closed or self.root_pending or grant.get("revoking") or self.grants.get(grant["id"]) is not grant):
            raise wire.WireError("locally_blocked")
        identity = self.identity
        private = identity["private"] if purpose == "installation" else grant["boot"]["private"]
        packet = wire.packet(private, purpose, identity["root"], grant["id"], command)
        result = await self.post("/command", packet)
        payload = wire.verify(result, grant["authority_key"], "authority", identity["root"], grant["id"])
        if not cleanup and (self.closed or self.root_pending or grant.get("revoking")):
            raise wire.WireError("locally_blocked")
        if payload.get("request_nonce") != packet["metadata"]["nonce"] or payload.get("error"):
            raise wire.WireError("remote_request_denied")
        return payload.get("value", {})

    async def review(self, user, code, authority_key):
        await self.setup_identity()
        wire.unb64(authority_key, size=32)
        codes = [code]
        combined = type(code) is str and code.startswith("psa1.")
        if combined:
            bundle = wire.loads(wire.unb64(code[5:], maximum=4096), maximum=4096)
            if set(bundle) != {"invitations"} or type(bundle["invitations"]) is not list or len(bundle["invitations"]) != 2:
                raise wire.WireError("invitation_denied")
            codes = bundle["invitations"]
        selections = []
        root = self.identity["root"]
        for invitation in codes:
            parts = invitation.split(".") if type(invitation) is str else []
            if len(parts) != 4 or parts[0] != root or not wire.ID.fullmatch(parts[1]):
                raise wire.WireError("invitation_denied")
            response = await self.post("/descriptor", dict(zip(("root", "grant", "invitation", "challenge"), parts)))
            descriptor = wire.verify(response, authority_key, "authority", root, parts[1])["descriptor"]
            registration = descriptor["registration"]
            if (descriptor["expires_at"] <= time.time() or registration["root"] != root
                    or registration["grant"] != parts[1] or parts[1] in self.grants
                    or any(item["parts"][1] == parts[1] for item in selections)):
                raise wire.WireError("invitation_denied")
            selections.append({"parts": parts, "descriptor": descriptor})
        if combined:
            first, second = (item["descriptor"] for item in selections)
            if ({first["ceiling"]["kind"], second["ceiling"]["kind"]} != {"ha_read", "optimizer_contribution"}
                    or any(first["registration"][key] != second["registration"][key]
                           for key in ("root", "actor", "profile", "public_key", "processor"))
                    or any(first["ceiling"][key] != second["ceiling"][key]
                           for key in ("actor", "client", "processor"))):
                raise wire.WireError("invitation_denied")
        if self.root_pending or not self.identity or self.identity["root"] != root:
            raise wire.WireError("invitation_denied")
        challenge = secrets.token_hex(32)
        self.reviews = {challenge: {"user": user, "expires": time.time() + 300,
            "selections": selections, "authority_key": authority_key, "combined": combined}}
        descriptors = [item["descriptor"] for item in selections]
        return {"challenge": challenge, "descriptor": descriptors[0], "descriptors": descriptors, "combined": combined}

    async def confirm(self, user, challenge):
        async with self.lock:
            review = self.reviews.pop(challenge, None)
            if not review or review["user"] != user or review["expires"] <= time.time() or self.root_pending or self.closed:
                raise wire.WireError("review_required")
            selections = review["selections"]
            if not self.identity or any(item["parts"][0] != self.identity["root"]
                    or item["parts"][1] in self.grants or item["descriptor"]["expires_at"] <= time.time()
                    for item in selections):
                raise wire.WireError("review_required")
            attestation = wire.secret_hash(wire.json_bytes({"user": user,
                "descriptors": [item["descriptor"] for item in selections], "challenge": challenge}).decode())
            additions = {}
            for item in selections:
                root, grant_id, invitation, challenge_secret = item["parts"]
                descriptor = item["descriptor"]
                grant = {"id": grant_id, "authority_key": review["authority_key"], "ceiling": descriptor["ceiling"],
                    "registration": descriptor["registration"], "revision": secrets.token_hex(32),
                    "continuation": secrets.token_urlsafe(32), "generation": 1, "revoke": secrets.token_urlsafe(32),
                    "attestation": attestation, "confirmed": True, "active": False,
                    "approval": {"invitation_hash": wire.secret_hash(invitation), "challenge_hash": wire.secret_hash(challenge_secret)}}
                if review["combined"]:
                    grant["approval_set"] = attestation
                additions[grant_id] = grant
            previous = self.grants
            self.grants = {**previous, **additions}
            try:
                # One atomic local write records the complete owner-approved selection.
                # Remote purpose-specific grants can connect independently afterward.
                await self.save()
            except BaseException:
                self.grants = previous
                raise
            self.start()
            return {"status": "approved_locally_connecting", "grant": next(iter(additions)), "grants": list(additions)}

    async def approve(self, grant):
        if "approval" not in grant:
            return
        try:
            approved = await self.command(grant, {"type": "approve", **grant["approval"],
                "ceiling_digest": grant["ceiling"]["policy_digest"], "installation_key": self.identity["public"],
                "continuation_hash": wire.secret_hash(grant["continuation"]), "revoke_hash": wire.secret_hash(grant["revoke"]),
                "revision": grant["revision"], "owner_attestation_digest": grant["attestation"]})
        except wire.WireError:
            status = await self.command(grant, {"type": "owner_status"})
            if status.get("status") != "active" or status.get("revision") != grant["revision"] or status.get("owner_attestation_digest") != grant["attestation"]:
                raise wire.WireError("approval_unconfirmed") from None
            approved=status
        grant['epoch']=approved['epoch']
        grant.pop('approval',None)
        await self.save()

    async def boot(self, grant):
        if grant.get('resume_wait') and not grant.get('boot_pending'):
            status = await self.command(grant, {'type': 'owner_status'})
            current = status.get('boot')
            if current and current.get('state') == 'active' and current.get('expires_at', 0) > time.time():
                return False
            grant.pop('resume_wait', None)
        if not grant.get("boot_pending"):
            private, public = wire.keypair()
            grant["boot_pending"] = {"private": private, "public": public, "attempt": secrets.token_hex(32),
                                     "old": grant["continuation"], "next": secrets.token_urlsafe(32), "generation": grant["generation"]}
            await self.save()
        pending = grant["boot_pending"]
        await self.command(grant, {"type": "boot", "generation": pending["generation"],
            "continuation_hash": wire.secret_hash(pending["old"]), "next_hash": wire.secret_hash(pending["next"]),
            "attempt": pending["attempt"], "key": pending["public"]})
        grant["continuation"], grant["generation"] = pending["next"], pending["generation"] + 1
        grant["boot"] = {key: pending[key] for key in ("private", "public", "attempt")}
        grant["boot"]["sequence"] = 0
        await self.save()
        result = await self.command(grant, {"type": "activate", "attempt": pending["attempt"],
                                           "continuation_hash": wire.secret_hash(pending["next"])})
        grant["lease"] = result["expires_at"]
        grant["active"] = True
        del grant["boot_pending"]
        await self.save()
        return True

    async def boot_command(self, grant, command):
        grant["boot"]["sequence"] += 1
        command = {**command, "attempt": grant["boot"]["attempt"], "sequence": grant["boot"]["sequence"]}
        # Sequence uncertainty loses a boot, never retries an ambiguous read.
        try:
            return await self.command(grant, command, "boot")
        except Exception:
            grant["active"] = False
            raise

    async def poll(self, grant):
        result = await self.boot_command(grant, {"type": "heartbeat"})
        grant["lease"] = result["expires_at"]
        page = await self.boot_command(grant, {"type": "poll", "permit": secrets.token_hex(32)})
        if page.get("status") == "idle":
            return
        from ..ha_read.reader import ReadContext
        binding = page["binding"]
        def allowed(context, operation):
            return (not self.closed and not self.root_pending and not grant.get("revoking") and grant.get("active") is True
                and time.time() < min(grant["lease"], page["expires_at"])
                and binding["grant_id"] == grant["id"] and binding["revision"] == grant["revision"]
                and binding['epoch']==grant.get('epoch',1) and binding['proof_key']==grant['registration']['public_key']
                and all(binding[key] == grant["ceiling"][key] for key in ("actor", "client", "processor", "audience")))
        context = ReadContext(self.identity["root"], binding["actor"], binding["client"], binding["epoch"],
            min(grant["lease"], page["expires_at"]), frozenset(grant["ceiling"]["operations"]), allowed,
            grant["id"], grant["revision"], grant["boot"]["attempt"])
        from ..ha_read.reader import ReadError
        try:
            result = await self.backend.read(context, page["operation"], page["arguments"])
        except ReadError as error:
            result = {'schema_version': 1, 'untrusted_content': True, 'error': {'code': str(error)}}
        context.check(page["operation"], time.time())
        text=wire.json_bytes(result).decode()
        await self.boot_command(grant, {"type": "stage", "id": page["id"], "permit": page["permit"],
            "result": text, "result_digest": wire.secret_hash(text)})

    async def revoke(self, grant_id=None):
        self.reviews.clear()
        if grant_id is None:
            if not self.grants and self.root_pin is None:
                self.identity = None
                await self.save()
                return
            self.root_pending = True
            if self.grants:
                first = next(iter(self.grants.values()))
                self.root_pin = {key: first[key] for key in ("id", "authority_key")}
        selected = self.grants.get(grant_id, {})
        approval_set = selected.get("approval_set")
        for key, grant in self.grants.items():
            if grant_id is None or key == grant_id or approval_set and grant.get("approval_set") == approval_set:
                grant["active"] = False
                grant["revoking"] = True
                grant['invitation_pending']='approval' in grant
                for secret in ("continuation", "boot", "boot_pending", "approval"):
                    grant.pop(secret, None)
        await self.save()
        self.start()

    async def drain(self):
        if self.root_pending and self.identity:
            grant=self.root_pin
            if grant:
                packet=wire.packet(self.identity['private'],'installation',self.identity['root'],grant['id'],
                    {'type':'root_revoke','installation_key':self.identity['public']})
                result=await self.post('/root-revoke',packet)
                payload=wire.verify(result,grant['authority_key'],'authority',self.identity['root'],grant['id'])
                if payload.get('request_nonce')!=packet['metadata']['nonce']:
                    raise wire.WireError('revoke_unconfirmed')
                value=payload.get('value',{})
                if value.get('cleanup')!='confirmed':
                    self.last_error='access_revoked_private_cleanup_pending'
                    return
                self.identity=None;self.grants.clear();self.root_pending=False;self.root_pin=None
                await self.save()
            return
        for key,grant in list(self.grants.items()):
            if grant.get('revoking'):
                value=None
                if grant.get('invitation_pending'):
                    try:
                        value=await self.command(grant,{'type':'cancel_invitation','installation_key':self.identity['public']})
                    except wire.WireError:
                        # Approval may have committed before its response was
                        # lost. The independently persisted revoke proof closes it.
                        pass
                if value is None:
                    value=await self.command(grant,{'type':'revoke','revoke_hash':wire.secret_hash(grant['revoke'])})
                if value.get('cleanup')!='confirmed':
                    self.last_error='access_revoked_private_cleanup_pending'
                    continue
                del self.grants[key]
                await self.save()

    async def loop(self):
        while not self.closed:
            try:
                async with self.lock:
                    await self.drain()
                    for grant in list(self.grants.values()):
                        if grant.get("revoking"):
                            continue
                        await self.approve(grant)
                        if not grant.get("active"):
                            if await self.boot(grant) is False:
                                continue
                        if grant["ceiling"]["kind"] == "ha_read":
                            await self.poll(grant)
                        else:
                            result = await self.boot_command(grant, {"type": "heartbeat"})
                            grant["lease"] = result["expires_at"]
                            job = await self.command(grant, {"type": "job_poll"}, "boot")
                            if job.get("state") == "ready" and (self.capture_task is None or self.capture_task.done()):
                                from .private_capture import FreshCapture
                                async def capture_job(g=grant, j=job):
                                    with suppress(Exception):
                                        await FreshCapture(self).run(g, j)
                                self.capture_task = asyncio.create_task(capture_job())
                if not self.root_pending and not any(grant.get("revoking") for grant in self.grants.values()):
                    self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "connection_unconfirmed"
            await asyncio.sleep(2)

    def status(self):
        return {"identity": {"root": self.identity["root"], "public_key": self.identity["public"]} if self.identity else None,
            "root_revocation": "pending_central_confirmation" if self.root_pending else None,
            "connection": self.last_error,
            "grants": [{"id": key, "approval_set": value.get("approval_set"), "ceiling": value["ceiling"], "activity":value.get('activity',{}), "status": "revoking_locally_blocked" if value.get("revoking") else "connected" if value.get("active") else "connecting"} for key, value in self.grants.items()]}

    async def stop(self):
        self.closed = True
        for grant in self.grants.values():
            grant["active"] = False
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.capture_task:
            self.capture_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.capture_task
        for grant in self.grants.values():
            if grant.get("boot") and not grant.get("revoking"):
                with suppress(Exception):
                    await self.boot_command(grant, {"type": "close_boot"})
        await self.save()
