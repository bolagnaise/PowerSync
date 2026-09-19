<!-- release: v2.12.1319 -->

## What's Changed

**Tesla Powerwall: restore safely after a failed charge-kick retry**
When a Powerwall has not started charging, PowerSync can retry its temporary
self-consumption → autonomous mode bounce. If that second bounce failed to
return to autonomous mode or confirm grid charging, the failure previously
skipped restoration and continued waiting for charging. Both bounce attempts
now invoke the same normal-restoration path on failure, and verification stops
once recovery begins. Existing saved settings, pending-restore persistence and
bounded restore retries remain in use.

**Protect newer control commands during recovery**
Recovery checks ownership again after sending its alert, so a newer command
that arrives during notification delivery is not overwritten by stale cleanup.
The kick timing, grid-readback requirements and physical charging checks are
unchanged. Successful API calls still do not establish that charging started.

The new runtime regression reproduces the missed second-bounce cleanup on the
previous release. Coverage includes failed mode writes, mismatched readback,
failed grid confirmation, restoration failure, superseded commands, and normal
charging/full-battery cases. This fixes a confirmed code path; it does not prove
the cause of every reported late start. Update through HACS and restart Home
Assistant before retesting scheduled charging.

Update available via HACS
