<!-- release: v2.12.1342 -->

## What's Changed

**Fail closed for disconnected Generic EV starts**

Generic EV Price-Level charging now treats explicit disconnected states such as
`not_plugged_in` and `unplugged` as unavailable. Missing or unavailable charger
switch entities are rejected before a service call, and Price-Level starts are
not promoted to an active session, notification, or home-battery preserve state
until a fresh charging status or positive power readback confirms the start.

Update available via HACS.
