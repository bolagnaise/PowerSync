# HA-wide read diagnostics

The HA administrator enables ongoing diagnostics from **HA diagnostics** in the
PowerSync dashboard. Codex and Claude Code use separately registered local stdio
bridges. Optimizer contribution has its own approval and data policy.

## Sources and bounds

The cached-state reader covers all entity domains. Installed adapters cover
entity/device/area/floor/label registries; bounded Recorder history, statistics
and raw logbook events; loaded automation/script definitions; cached/saved traces;
integration data/options; YAML configuration; loaded/YAML/storage dashboards;
core, PowerSync and system logs; cached system, Supervisor and backup metadata.
The audited adapter API baseline is Home Assistant Core 2026.11.1. Optional
components, missing caches and unsupported minor versions report unavailable.

Readers use passive sources and fixed queries. Configuration uses descriptor-
relative file access, refuses symlinks/traversal/secret files, and parses YAML
without constructing tags, resolving includes or rendering templates. Logbook
reads avoid third-party humanification callbacks. Backup contents are excluded.

Pages are bounded by rows and bytes. Cursors bind the query, inventory revision,
installation, actor, client, grant revision, epoch and process boot. Permissions
are checked before and after awaited work. Cancelled executor work retains its
concurrency slot until the underlying thread actually finishes.

Known credential fields and known installation credential values are redacted.
The consent disclosure explains that arbitrary free text can contain private
information. Responses are marked untrusted content.

## Transport and ownership

The dedicated `diagnostics.powersync.cc` Worker has its own installation authority
and no product database or actuator binding. Installation/client/boot/runner
proofs have distinct signing domains. Root revocation, role policy, sender keys,
revision/epoch and final disclosure are checked against current authority.
Read arguments/results exist only in memory; a cold authority requires a fresh
read. Private contribution ciphertext also stays out of durable storage/backups.

The HA administrator reviews the exact client, processor, purpose and disclosure
before confirming. A local revoke immediately blocks further reads. HA retains
cleanup-only capability until central revocation and private purge are confirmed.
A pending cleanup is visible and does not become successful merely by expiring.

## Compatibility and delivery

Synthetic real-HTTP/workerd tests cover the owner journey, separate client
profiles, restart, independent/root revocation, reserved capture, encrypted
transfer, actual isolated replay and purge. Native discovery was checked with
Codex CLI 0.166.0 app-server and Claude Code 2.1.319. The pilot transport is local
stdio. [Operations](private-fleet-operations.md) covers provisioning, deployment,
recovery, packaging and the separate real-owner acceptance gates.
