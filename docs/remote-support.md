# Home Assistant support

## Share diagnostics

PowerSync Support lets a Home Assistant administrator share selected read-only
diagnostics with an assigned investigator. Sharing is optional and requires
approval in Home Assistant.

Open **Support** in the PowerSync HA dashboard using an administrator account.
Use the local diagnostic download when a remote session is unnecessary.

The same card offers temporary sessions and **Allow ongoing support**. Leave
that option off for the temporary workflow below. Select it for ongoing
read-only diagnostics and any private offline testing included in the reviewed
invitation; see [Diagnostics and privacy](diagnostics-privacy.md) for setup,
scope and retention. Selecting or clearing the option grants and revokes
nothing. Existing temporary and ongoing access remains manageable in this card.

For a temporary remote session arranged through your private support ticket:

1. Paste the investigator's invitation into **Support invitation** and select
   **Get pairing code**.
2. Use **Choose what to share** to review categories, history and duration.
   All six diagnostic feeds are selected by default for one hour, without past
   history. You can deselect feeds before pairing. HA Recorder history remains
   a separate choice.
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

If support initialization times out during a busy HA startup, PowerSync retries
once after HA has finished starting. This does not restore a temporary session
or approve new access. If the card still returns `404: Not Found`, include the
PowerSync support startup warning from the HA log in your existing ticket.

## Approve a temporary administrator for a repair

Use **Temporary administrator access** in the **Support** card when you have
arranged a repair with a verified investigator. Sign in as the Home Assistant
owner. The investigator needs a trusted HTTPS address already configured for
your Home Assistant installation and reachable from their support client.

1. Ask the investigator for the challenge for this repair. Confirm the
   investigator and the intended work in your existing support conversation.
2. Paste the challenge into **Investigator challenge**, review the access
   description, and tick the approval box. Select **Approve temporary
   administrator access** to grant access or **Deny administrator access**
   to clear the request.
3. The investigator has five minutes to connect. Home Assistant then creates a
   temporary administrator. That account can read or change what a Home
   Assistant administrator can access through its APIs, including
   integrations, automations and device controls. Its short-lived credentials
   renew automatically while the investigator remains connected. You approve
   access once for the repair.
4. Ask the investigator to log out when finished, or select **Stop
   administrator access** at any time. Check the card for confirmation.

The investigator does not need your password or a long-lived access token.
Home Assistant removes the temporary account on logout, on revocation, or
when a disconnected session stops renewing. Restarting Home Assistant also
ends the session. Ending access leaves repairs and configuration changes in place.

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

Tesla Fleet command preparation is included in **Recorded control observations**
and **Structured diagnostic events** while local recording or an approved session
with either feed is active. It records the refusal reason, source-binding result,
vehicle state and charge-data timestamps before and after the existing refresh.
These observations do not wake the vehicle or send commands. A successful
preparation check is a decision, not proof that a command was sent or charging
started. No VIN, entity name, credentials or provider error text is shared.

Recorded control observations also include the runtime check blocking native
Powerwall site-limit changes, such as an active manual control, a monitoring-only
connection, or pending recovery. This status reads PowerSync's cached control
state without polling the gateway or changing settings. An allowed runtime check
does not establish fresh gateway configuration or successful hardware enforcement.
