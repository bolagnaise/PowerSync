# Read-only support diagnostics

PowerSync Support lets a Home Assistant administrator share selected diagnostics
with an assigned investigator. Sharing is optional and requires approval in HA.
It does not permit shell access, settings changes or hardware commands.

## Share diagnostics

Open **Support** in the PowerSync HA dashboard using an administrator account.
Use the local diagnostic download when a remote session is unnecessary.

For a remote session arranged through your private support ticket:

1. Paste the investigator's invitation into **Support invitation** and select
   **Get pairing code**.
2. Use **Choose what to share** to review categories, history and duration.
   The default shares versions, safe settings and energy readings for one hour,
   without past history.
3. Select **Copy pairing code** and send it through the same private ticket.
   A pairing code alone does not approve diagnostic reads.
4. When **Review and approve** appears, check the investigator, reporter,
   ticket, matching phrase, scope and processing policy. Select
   **Approve read-only access** only if they match your intended session.
5. Select **Stop support access** when finished, or **Cancel setup** before
   approval. A restart or integration unload closes the live session.

The card distinguishes a local stop from central cleanup that is still pending.
Approval must be completed within the displayed setup window. Refreshing the
card does not approve access.

## Local recording and shared archives

Local recording is off by default and separate from remote sharing. Its controls
and local downloads are under **Local diagnostics and recording**; access
receipts appear under **Recent support activity**. Persistent recording is a
separate opt-in and may be included in Home Assistant backups.

Sharing a recorded incident archive requires separate approval. An approved
archive can remain available for its stated period while HA is offline; ending
live access does not revoke it. Use **Revoke shared archive** to revoke retained
archives and check for any pending cleanup.

Diagnostic values can be incomplete or stale. A command acknowledgement does
not prove a physical hardware response. Read the sharing policy shown in HA
before approving access; revocation cannot retract copies already received by
an investigator or an approved processor.

Ongoing diagnostic access and private offline optimizer testing have their own
approval and retention policy in [Diagnostics and privacy](diagnostics-privacy.md).
