<!-- release: v2.12.1584 -->

## Accounting history saves during normal operation

Fixed continuous polling postponing saves of battery energy origin, daily costs, solar forecast learning and battery efficiency learning. Pending saves now keep their five-minute deadline and include the latest data, reducing the history lost after an unexpected shutdown.

Update through HACS and restart Home Assistant. Previously lost history cannot be recovered; unmeasured battery energy remains marked as unknown origin.
