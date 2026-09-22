<!-- release: v2.12.1326 -->

## What's Changed

**Tesla Powerwall: scheduled grid charging no longer reverts itself at the start of a cheap window**
When a cheap or free window opens, PowerSync switches the Powerwall to
autonomous mode, turns on charging from the grid, sets the reserve and uploads
the tariff. It will not leave a window half-applied, so if it cannot confirm
that grid charging is on it undoes the whole attempt and lets the next
optimization cycle try again — deliberately fail-closed, because silently
believing grid charging is on when it is not would waste the window.

Confirmation is done by reading Tesla's `site_info` back directly, up to five
times within ten seconds. The problem is what Tesla returns: on many sites the
grid-charging field is simply **left out** of `site_info` while charging from
the grid is allowed. So the moment an enable actually succeeds, the field PowerSync
is looking for disappears rather than turning true, and the write can never be
confirmed by reading its own value. There is already a compatibility path for
this, but it only accepted the result when *every* readback in the window
omitted the field.

That last condition is what broke. Tesla's cloud API serves the old value for a
second or two after accepting a write, so the first readback usually still
carries the previous state — with the field present. One such stale read was
enough to fail the unanimity check, so a grid-charging enable Tesla had already
accepted and applied was scored as unverified and then actively undone, followed
by another attempt into the same trap. Each round trip cost roughly 20–25
seconds, and the window only started once an attempt happened to land its first
readback after Tesla had caught up. On a reported Globird Zero Hero 11:00 free
window this delayed the start by about six minutes across four reverted
attempts, with a "Force Charge Retrying" notification for each one.

PowerSync now reads the confirmation in order: for an enable, the **newest**
valid readback decides, so a stale first read no longer overrides the omission
that follows it. The guards around that are unchanged or tighter: a readback
that positively contradicts the request still fails, a structurally invalid or
empty `site_info` response still fails, a truncated response that never carried
the system-components block is not read as an omission at all, at least two
valid readbacks are still required, and the whole fail-closed revert still runs
when confirmation genuinely does not arrive. Turning grid charging **off** is
unchanged and still requires every readback to agree — an omitted field does not
describe a disable, and sites that never expose the field at all must still be
able to restore normal operation.

The "could not confirm" warning now also records how many readbacks were valid,
how many omitted the field, and the last value actually seen, so a log from a
site that still fails shows which of those causes it was.

This affects Tesla Powerwall sites using cloud control, which is also the path
taken whenever the local gateway is unreachable. If your Powerwall's local
connection is down, every write and readback goes through Tesla's slower cloud
API, which is where this was most visible.
