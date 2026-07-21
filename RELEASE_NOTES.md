<!-- release: v2.12.904 -->

## What's Changed

**Fresh optimizer actions now start at tariff boundaries**
PowerSync now allows a newly solved charge or export action to replace the cached non-force action when the solve completes within the first 30 seconds of a five-minute boundary. This prevents a fresh Flow Power Happy Hour export from being deferred for the full slot just because the previous plan still said self-consumption at the instant the boundary arrived.

**Late solve stability remains protected**
Periodic solves that finish later in the slot still preserve the already-applied cached action until the next boundary, avoiding disruptive mid-slot mode changes while fixing the normal one-to-two-second solve path.

Update available via HACS
