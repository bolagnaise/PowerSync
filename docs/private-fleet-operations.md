# Ongoing diagnostics and private regression operations

The released software is disabled until configured and approved. One owner
confirmation now covers ongoing diagnostic support and private offline optimizer
testing for the registered application. The underlying read and contribution
grants retain independent technical scopes and connect in the background;
combined access is shown as connected only when both are connected. Installing
or updating PowerSync does not create approval. Existing read-only approvals
keep their original scope until the owner reviews a combined invitation.

## Delivery and acceptance

Keep separate receipts for HA source/publication, the isolated Worker deployment,
bridge compatibility, private runner activation, each owner's consent, actual
participation, measured physical calibration and seven elapsed observation days.
Synthetic virtual-time tests establish software behavior. They cannot populate
the real calibration or observation acceptance fields.

The supported local clients were checked with Codex CLI 0.166.0 app-server and
Claude Code 2.1.319. The HA adapter API baseline is Core 2026.11.1. Capabilities
show optional/unavailable sources and unsupported HA minor versions explicitly.
The pilot uses local stdio bridges and signed outbound HTTPS.

## Isolated authority deployment

The Cloud repository contains `worker/wrangler.diagnostics.toml` and the manual
**Deploy isolated HA diagnostics** workflow. Its default is disabled, with empty
operator/key configuration. The deployment has only its own Durable Object and
rate limiter. It has no product database, vendor credentials, actuator binding,
raw object storage or payload logging.

Before enabling a pilot, independently configure the authority signing secret
`DIAGNOSTICS_SIGNING_SEED`, matching `DIAGNOSTICS_PUBLIC_KEY`, an independent
`DIAGNOSTICS_EPOCH`, immutable human Access subjects in `DIAGNOSTICS_ACTORS`, the
Access team/application audience and registered runner public keys/generations.
Protected public policy is rendered through `scripts/diagnostics/render_config.py`.
Signing seeds never belong in runtime variables, source, artifacts or DO backups.

Protect the `/invite` operator page with Cloudflare Access. Signed client/HA
transport endpoints authenticate their own proofs. The operator page registers
a public client key and creates a one-use invitation for the exact HA identity,
client and purpose internally. The operator page combines the read and testing
invitations into one owner-facing invitation. Give that invitation and the
authority public key to the HA admin. If invitation creation fails midway, no
access has been approved; retry setup rather than treating either invitation as
an active grant.

Rotate the independent epoch before reopening restored authority storage. A
restored DO snapshot must not restore old access. Retain cleanup-only credentials
until all pending private purge receipts have been reconciled.

## Owner approval and client setup

Open a support ticket for initial enrollment. The operator verifies the reporter
and HA installation before creating the ongoing-access invitation. After approval,
private tests of identified code changes may use the contribution grant without
an active bug ticket. Incident-specific diagnostic reads require a current active
ticket linked to the same owner and installation.

1. In the HA administrator's **HA diagnostics** card, create a setup identity.
   This displays public setup information and grants no access.
2. Create a separate client key file for each profile. Use an owner-only directory
   and Python with the bridge's pinned `cryptography` dependency:

   ```sh
   PYTHONPATH=/opt/powersync-diagnostics-bridge.zip python -m diagnostics_access.provision create --profile codex --output /private/codex-key.json
   ```

   Register the displayed public key with the operator. Repeat for `claude`.
3. Paste the resulting invitation and authority public key into HA. Review the
   connected application, operator and disclosure for both support and private
   testing; then choose **Enable ongoing access** once. Approval survives restarts
   until revoked. The operator receives both technical grant IDs. Use the read
   grant for the MCP configuration and the contribution grant for private replay.
   A legacy single-purpose invitation retains its original explicit scope.
4. Bind the client key to the approved root/grant/public authority identity:

   ```sh
   PYTHONPATH=/opt/powersync-diagnostics-bridge.zip python -m diagnostics_access.provision bind --key-file /private/codex-key.json --output /private/codex.json --root ROOT_ID --grant GRANT_ID --authority-key PUBLIC_KEY
   ```

5. Configure Codex's MCP command as the Python executable with arguments
   `[/opt/powersync-diagnostics-bridge.zip, --profile, codex, --config,
   /private/codex.json]`. Use the corresponding `claude` profile/config in
   Claude Code's stdio MCP registration. Neither bridge has a listening port or
   a fallback bearer/HA token. Keep its configuration owner-only and unshared.

Read responses are bounded, redacted and marked untrusted. Arbitrary free text
may still contain personal information. Diagnostic queries/results exist only
in memory; after an authority restart the client requests a fresh read.

## Private participant index and ticket lookup

There is no automatic global Discord-to-installation directory in the authority.
Keep the operator index on the investigation host, outside source and public
reports. During enrollment, the human operator verifies the immutable Discord
reporter ID and exact HA setup identity through the private ticket. Record the
message establishing that association, then register each already-approved
read/testing configuration using the shipped operator helper:

```sh
python scripts/diagnostics_enrollments.py enroll --reporter-id DISCORD_USER_ID --ownership-reference https://discord.com/channels/1443943629130567874/CHANNEL_ID/MESSAGE_ID --config /private/read-client.json
python scripts/diagnostics_enrollments.py enroll --reporter-id DISCORD_USER_ID --ownership-reference https://discord.com/channels/1443943629130567874/CHANNEL_ID/MESSAGE_ID --config /private/testing-client.json
python scripts/diagnostics_enrollments.py lookup --reporter-id DISCORD_USER_ID
```

The default index is `~/.local/share/powersync-diagnostics/enrollments.json`
inside an owner-only directory. The directory stores verified ownership linkage,
public grant identifiers, purposes, a local configuration-file reference and last
checked metadata. It contains no credentials, HA settings, logs or raw captures.
Credential files remain owner-only and outside the index. The reference records
human verification; supplying a message URL alone does not establish ownership.
Conflicting owner associations are rejected for operator reconciliation.

Lookup rechecks each matching grant with the signed live authority. An old row
never authorizes access; revoked or unreachable grants remain unavailable. An
active grant does not prove HA is online: the subsequent bounded read must also
complete. Reuse valid diagnostic approval for new tickets without asking the
owner to pair again. Do not guess a match from a display name or ticket number.
Missing linkage is unlinked, not proof that the owner has never approved access.
No real participant is enrolled by installing the package or running synthetic
acceptance tests.

For the investigation and cross-configuration verification workflow, read
[Testing against approved installations](testing-approved-installations.md).

## Immutable paired runner

Use the public runner sources with the exact reviewed HA repository checkout.
Build each baseline/candidate image with the trusted builder and an immutable
Python base digest matching the installation's Python/solver dependency profile:

```sh
python scripts/private_regression/build.py --source /reviewed/baseline --output /artifacts/baseline --base python@sha256:REVIEWED_DIGEST
```

The builder freezes source and records component/runtime/scorer/schema/image
identities. The controller verifies the actual source bytes copied from each
image and executes a private-profile health probe before reserving capture.
An incompatible runtime does not receive an input bundle.

Each pair manifest names baseline/candidate `root`, immutable `image` and
`component` identities; the installation `source` runtime identity; `runtime`,
`scorer`, `schema`, `currency`, and `required_profiles`. A complete campaign
requires `solver_v1`, `reserve-recovery-temporal-v1` and `calibrated-plant-v1`,
plus an explicit `terminal_price` in that currency. Keep forecasts, observed
meters and counterfactual scores distinct.

Only one local private job is admitted at a time. The host must be Linux 6.9 or
newer with pidfd process-group signalling, with
swap disabled and no piped core handler. Private processes are non-dumpable.
Containers run as UID 65534 with no network/host/scorer mounts, a read-only root,
bounded tmpfs, dropped capabilities, no-new-privileges, memory/process/CPU/wall
limits and no persistent logs. Inputs and results travel over bounded pipes.
The disposable trusted process has its own process group. Cancellation and the
watchdog kill that group through a held pidfd, including Docker CLI children
that could otherwise retain pipe data after their parent dies. Cleanup also
checks the exact public job markers of disposable scorer and attachment processes
through pidfds before confirming deletion, including stopped orphaned children.

Install the independent reaper timer before activating the runner service.
Templates are in `scripts/private_regression/systemd/`. The service configuration
names the immutable pair/campaign, private cohort key, metadata ledger, separately
approved contribution client files and an interval of at least 900 seconds.
Service activation and Docker access are explicit operator setup steps.
Pin `daemon_id` in every private runner client configuration to the public value
from `docker info --format '{{.ID}}'`. Recovery checks that identity before trusting
an empty container listing. Pin `pid_namespace` to `readlink /proc/self/ns/pid`
in those client files too; do not reuse them in another PID namespace. Put the same value in the reaper's owner-only
`/etc/powersync-regression/host.env` as `POWERSYNC_DOCKER_ID=...`.
The reaper refuses PID-owner checks across PID namespaces. Install it in the
runner's PID namespace and verify that it removes an abandoned synthetic job.

## Capture and private data lifetime

A healthy reserved runner/key and exact pair are required before HA attaches an
observer. Capture occurs during a subsequent normal optimization. Private data
uses job-bound X25519/HKDF/AES-GCM encryption and bounded memory-only relay.
The absolute job lifetime is 30 minutes. There is no raw archive or unattended
backlog, including encrypted bundles, normalized state or trajectories.

The stateful profile observes selected real reserve-restoration calls and their
acknowledgements. Its offline driver includes ordered events, timers and restart
fences. Unobserved provider, EV, force and command paths remain insufficient.
An acknowledgement does not establish a physical effect.

The physical profile samples fresh cached, coherent battery/site meters and
known units/signs. It needs independent charge/discharge observations, adequate
SOC resolution, a separate time-blocked withheld set and known physical reserve.
Short, single-direction, stale or out-of-range captures cannot validate a model.
Simulation outside the calibrated range remains insufficient; forecast-based
scores never become claims of observed savings.

The trusted scorer validates candidate output, checks reconstruction and A/A
for both arms, and computes independent scores. Crashes, timeouts, malformed
numbers and nondeterminism are accounted for. A candidate failure alongside a
healthy baseline is a failed comparison. Infrastructure/baseline failures create
invalid or missing work and block campaign acceptance.

Raw input, effective state, private calibration data and trajectories disappear
with the disposable private process and its containers. The independent pidfd
watchdog and host reaper cover controller death, deadlines and abandoned jobs.
A summary is eligible only after a real purge confirmation. The service retains
only bounded trusted summaries for seven days and coordination metadata.

## Advisory campaigns and recovery

`campaign_cli.py new-key`, `freeze` and `report` manage the private cohort key,
immutable pair and consumed metadata ledger. Freeze consumes the holdout epoch
before feedback is inspected. A changed candidate requires a new epoch. Reports
recompute assignment, account for every reservation, require eligible daily
coverage per logical installation and independent holdout/development coverage,
and apply per-pair and site-weighted aggregate bounds. Repeated observations are
not additional households; distinct physical households remain unknown.

Plan the final admission cutoff before the observation window closes so its
last private jobs can finish and purge. Missing/invalid work and unavailable or
revoked grants cannot help a campaign pass. Fleet evidence is advisory alongside
the mandatory protected-main audited full-suite release gate.

Owners revoke the combined ongoing access or all installation access in HA.
Revoking either member of a combined approval blocks both purposes locally and
reconciles both revocations centrally. Legacy standalone grants retain their
individual revoke controls. Access stops
locally immediately; central authorization is fenced at the same authority
transaction as disclosure. Retained summaries are deleted on revocation.
Running jobs lose their lease and the private controller kills/purges them.
HA shows cleanup pending until the runner confirms deletion. Neither a deadline
nor metadata expiry is accepted as proof of purge.

After an interrupted runner, run the host reaper and then
`controller.py --config PRIVATE_RUNNER_CONFIG --recover-job EXACT_JOB_ID`.
This performs metadata-only reconciliation after actual host cleanup; it never
replays an old capture. Unconfirmed cleanup quarantines the runner and keeps the
owner's cleanup request pending. Resume with a fresh capture after reconciliation.
