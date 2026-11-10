# Read-only support diagnostics

PowerSync Support lets an HA administrator share selected cached diagnostics
with an assigned PowerSync investigator. It is independent of Cloud accounts,
subscriptions and the existing ChatGPT optimizer feature. There are no native
mobile settings or customer AI/chat endpoints for this feature.

The broker ships disabled. Until the operator configures and enables the separate
support service, local downloads and opt-in local recording work without it.
This document describes implemented behavior, not a production activation receipt.

## Administrator workflow

Open **Support** in the PowerSync HA dashboard using an administrator account.
Download local diagnostics first when a remote session is unnecessary. Native HA
integration diagnostics uses the same allowlisted collector.

For a remote session:

1. The investigator verifies the reporter and assignment in the private Discord
   ticket, then creates a 30-minute invitation in the protected operator viewer.
2. Paste that invitation into the full-width **Support invitation** field and
   select **Get pairing code**. The default shares versions, safe settings and
   energy readings for one hour, with no past history. **Choose what to share**
   lets you change categories, history range and duration (up to eight hours).
   Only one entry may have a local session at a time.
3. Select **Copy pairing code** and send it through the same private ticket.
   The code can also be selected and copied manually. Claiming it
   does not authorize diagnostic reads. Complete pairing and HA approval within
   30 minutes of preparing the session; the selected session deadline still applies.
4. While the card is open it checks status every five seconds. When support
   claims the code, **Review and approve** shows the investigator, reporter,
   ticket, matching phrase, exact sharing scope and expanded processing policy.
   Review them, check consent and select **Approve read-only access**. The
   30-minute setup window is a time limit, not a waiting period. Refreshing status
   never approves access for you; changed identity or scope requires fresh review.
5. Select **Stop support access** in HA when the investigation ends (or **Cancel
   setup** before approval). HA destroys its live key before any
   network request. The page distinguishes a local stop from a central revoke
   still awaiting connectivity. A restart, unload or mapping change closes local
   consent; restart never restores a live session.

Optional recording and local downloads are under **Local diagnostics and
recording**. Access receipts and queries are under **Recent support activity**.
Investigator email identities in the card and its access receipts display the
PowerSync support contact, `dev@powersync.cc`, including previously saved receipts.
The underlying operator identity remains bound to authorization and signed consent.
Archive sharing needs its own approval; **Revoke shared archive** remains
available after live access ends.

There is no shell, arbitrary file/URL/entity query, prompt, service call, device
refresh or control API. Reading diagnostics does not change battery, inverter,
EV, charger, optimizer, reserve or tariff configuration. An existing successful
command event remains an acknowledgement; it is not physical proof.

## Evidence and history

The closed schema admits reviewed numerical values, booleans and enums only.
Credentials, VINs, serials, account/location identifiers, URLs, raw payloads and
free text are excluded. Returned records remain untrusted data.

Snapshots read HA cache and stored optimizer fields. They report mixed source
coherence and self-asserted installation identity. Missing device observation
timestamps stay missing; collection time and HA report time do not make old data
fresh. Missing values are unavailable, never zero. Current expired force state
is copied without invoking cleanup/getters.

Optimizer solve observations retain bounded copies of reviewed core inputs
(prices in currency/kWh, solar/load forecasts, actions and constraints). They
explicitly set `input_bundle_complete=false`: provider/EV internals are outside
this bundle, so it does not promise full deterministic reconstruction. Structured
control events retain their original evidence level. Unsupported hardware
readback and physical observations are not invented. The logs category contains
structured events only; no raw HA logger handler is installed.

Local recording is off by default and separate from remote sharing. The recorder
keeps up to 24 hours, 6,000 rows and 8 MiB of serialized data across all entries,
with eviction/drop counters. Actual process memory is higher than serialized
bytes. Persistent recording is separately opt-in and saves at most once per
minute; HA backups can include it. Disabling recording clears local support
history. Mapping changes clear history, and restored records require the same
mapping digest. A restart loses RAM-only samples.

Requests cover at most one hour within the administrator's selected history
range. Historical frames are filtered again by approved categories. HA Recorder
import requires history, entities and the separate HA Recorder checkbox. It reads
at most seven approved entities, 1,000 changes each, without start-state padding,
using the Recorder executor. Entity registry creation/modification must predate
the requested interval; otherwise identity is unsupported. HA Recorder's own
retention remains under HA control.

## Live results and separately retained archives

If preparing an invitation fails before a pairing code is received, support
access closes locally and the invitation form becomes available again. No
diagnostic reads are permitted. An expired or already-used invitation requires
a fresh invitation from support; do not share invitation tokens publicly.

Live evidence stages for at most 60 seconds. Every page requires a new signed
five-second HA release. Disconnecting HA prevents new permits; a permit already
issued remains usable only until its original five-second expiry.
Requests expire after 30 seconds; collection has a five-second deadline. Results
are capped at 256 KiB and pages normally at 32 KiB; rows are never split. A large
single row can exceed the page target, within the result cap. Session upload
budget is 16 MiB and the broker permits 12 requests per minute.

The owner can separately share one immutable incident archive per session for up
to 24 hours. It includes the approved recorded categories and up to the preceding
hour within scope. Archive readers need a separate grant, and reads work while
HA is offline. Live revocation preserves separately consented archives. **Revoke
shared archive** revokes every retained archive, including uploads with lost
responses; failed revocations retry in the background. Delete-only credentials
are saved before upload and survive restart. They cannot read diagnostics or
use MCP. Session keys and read credentials never enter persistent support storage.
Removing the integration erases local support history and attempts remote revoke.
If offline, HA displays a cleanup warning and retains only delete capabilities;
staff can revoke centrally and any archive keeps its original deadline.

The displayed policy must disclose investigators, model processors, transcripts,
active-store expiry, 30-day metadata audit retention and backup/recovery residuals.
Revocation prevents future service reads; it cannot retract copies already
received by people, agents or downstream transcripts. Recent access receipts are
shown locally. Authorized staff can retrieve retained audit by session ID after
session closure. Diagnostic payloads are separate from metadata audits.

## Validation and activation

Run `python -m pytest tests/test_support_*.py` with the repository Python 3.12
environment, then the full audited regression runner in
[regression-release-gate.md](regression-release-gate.md). The paired Cloud
implementation and operational controls are documented in its
`agent_docs/remote_support.md`.

Activation requires a separate authorized pilot installation, real OAuth client
callback configuration, event-loop/memory/request-cost measurement, revoke/offline
and recovery drills, and the normal release/deployment gates. No live installation
or physical effect is established by synthetic tests or a successful build.
