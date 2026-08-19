<!-- release: v2.12.1147 -->

## Fixes a battery left half-restored after a restart

If Home Assistant restarted while PowerSync was force-charging your battery — an integration update, a reboot, anything — the restore that runs on the way out could fail its confirmation and then be abandoned. The battery dropped out of force charge and stopped charging, with nothing tracking the unfinished work.

### What was happening

When PowerSync restores normal operation and one of its Tesla writes doesn't confirm, it schedules a retry sixty seconds later and logs:

```
Tesla restore_normal did not fully complete: grid charging restore failed
Tesla restore_normal incomplete; retry 1 scheduled in 60 seconds
```

That retry is an in-process timer, so it dies with the process. On a restart it could never run. And on the restart-recovery path there was a second reason it would never help: the retry checks whether force state is still active before doing anything, and startup recovery has already cleared it — so even a surviving timer would have skipped.

The result was a restore that logged its own failure, promised a retry, and then quietly gave up.

### What changed

PowerSync now persists the *intent* rather than depending on a timer outliving the process:

- The unfinished restore is recorded as soon as the Tesla writes fail — unconditionally, not only when the in-process retry is successfully scheduled.
- The record is cleared once a restore genuinely completes.
- Startup completes any restore still outstanding, **including the case this previously missed entirely**: no force state left to re-arm, but a restore still owed.

The sixty-second in-process retry still runs as before when the process stays alive. This adds the durable path underneath it.

Batteries on other brands are unaffected — the marker is specific to the Tesla restore path, and the per-brand restores never set it.

### If you were affected

Symptom: your battery stopped charging shortly after a restart or an integration update, while the optimizer still showed charge slots in its plan. Putting the battery into Time of Use with a high backup reserve was a working manual workaround; you can revert that once you're on this release and let PowerSync take control back.
