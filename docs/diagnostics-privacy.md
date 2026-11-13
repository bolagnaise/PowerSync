# Diagnostics and privacy

PowerSync offers optional ongoing diagnostic support and private offline
optimizer testing. Both require configuration and approval by a Home Assistant
administrator. Installing or updating PowerSync does not enroll your
installation or grant access. Software availability does not mean that a
diagnostics service or testing pilot is active.

## What you approve

The **HA diagnostics** card shows the connected application, operator, processor,
purposes and disclosure for review before approval. Codex and Claude Code have
separate client registrations. Review the actual invitation for the application
and processor that will receive your information.

One confirmation can approve ongoing diagnostic support and private offline
testing together. These retain separate technical permissions, and combined
access is shown as connected only when both are connected. Existing read-only
approvals keep their original scope until you review and approve a combined
invitation. A legacy single-purpose invitation retains its stated scope.

Approval survives Home Assistant restarts until revoked. It avoids repeating
setup for each covered request or test; it does not authorize unrelated browsing,
settings changes, candidate installation or live hardware control.

## What diagnostic support can read

Diagnostic access can cover the wider Home Assistant installation, including
other integrations. Available sources include:

- Cached entity states and entity, device, area, floor and label registries.
- Bounded history, statistics and logbook events.
- Automation and script definitions, saved or cached traces, integration
  configuration, YAML configuration and dashboards.
- Core, PowerSync and system logs, plus cached system, Supervisor and backup
  metadata. Backup contents are excluded.

Readers use passive sources and bounded queries. Missing components, unavailable
data and unsupported Home Assistant versions are reported explicitly.
Configuration reads exclude secret files and do not resolve YAML includes or
render templates. See [HA-wide diagnostics](ha-read-backend.md) for the technical
source and transport boundaries.

Known credential fields and known installation credential values are redacted.
Arbitrary free text can still contain personal information, so redaction is not
a guarantee that every sensitive detail has been removed. Diagnostic responses
are marked as untrusted data. Relevant diagnostic excerpts may be used in the
approved application's investigation context; private raw optimizer captures
follow the separate policy below.

Incident-specific diagnostic reads require a current active support ticket
linked to the same owner and installation. Support should read the information
needed to investigate the issue, expanding to related sources when necessary.
See [Testing against approved installations](testing-approved-installations.md)
for the investigation and verification policy.

## What private offline testing uses

With testing approval, identified code changes can be compared against a
baseline using your installation's optimizer inputs and selected observations.
These can include resolved optimizer state, forecasts, battery/site meter
observations and selected reserve-restoration events and acknowledgements.
Testing can continue after the initial enrollment ticket closes.

Capture requires an approved, ready runner reserved for the exact comparison.
Candidate code runs offline in isolated, resource-limited containers without
network access. Private inputs are encrypted for the job and relayed in bounded
memory. They are not made available as raw captures through the Codex/Claude
bridge, committed as test fixtures or copied into public tickets or reports.

Private testing does not install the candidate on your Home Assistant system or
command your hardware. Supported profiles have explicit limits: a simulation,
software acknowledgement or successful replay does not prove a physical effect
or observed savings. Missing observations or calibration remain coverage gaps.
Testing supplements the normal release checks.

## Data lifetime and retained information

Diagnostic query arguments and responses exist only in the authority's memory;
after an authority restart the client requests a fresh read. This describes the
diagnostic transport, not the retention policy of the application or processor
identified in your approval disclosure.

Private testing uses replay, then delete. Jobs have an absolute lifetime of
30 minutes. Raw inputs, effective state, calibration data and trajectories are
discarded with the disposable private processes and containers. There is no raw
archive or unattended backlog, including encrypted captures. A deadline alone
does not count as confirmation that deletion succeeded.

Only bounded trusted testing summaries are retained for seven days after
confirmed purge, together with coordination metadata. Retained summaries are
deleted on revocation. This seven-day summary period does not describe the
lifetime of approval, ownership records or coordination metadata.

The operator separately maintains a private ownership index linking your
verified support identity and installation to approved purposes, public grant
identifiers and local configuration references. It does not store credentials,
HA settings, logs or raw captures. An index entry does not itself authorize
access; current permission is checked with the authority.

## Enable or revoke access

1. Open a support ticket to arrange enrollment and verify the owner and
   installation.
2. In the administrator's **HA diagnostics** card, create a setup identity.
   Creating this identity grants no access.
3. Enter the invitation and authority public key supplied through the verified
   support process. Review both purposes, the application, operator and
   disclosure, then choose **Enable ongoing access** if you agree.

You can revoke combined ongoing access or all installation access in HA.
Revoking either member of a combined approval blocks both purposes locally and
reconciles both revocations centrally. Legacy standalone approvals retain
individual revoke controls.

Revocation stops access locally immediately. Running private jobs lose their
lease and are stopped and purged. HA shows **cleanup pending** until the runner
confirms deletion. An expired job or unavailable service does not turn pending
cleanup into confirmed deletion. Contact support if cleanup remains pending.
