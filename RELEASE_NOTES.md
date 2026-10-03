<!-- release: v2.12.1343 -->

## What's Changed

**Keep Smart Schedule and deadline charging within site import limits**

Smart Schedule now preserves the lowest available positive import limit from
the EV session, Smart Optimization, Tesla site and Home Power settings. An
explicit session limit can reduce the site ceiling but cannot override a lower
configured limit.

**Check live headroom before starting every Smart Schedule charger**

The initial charging command now checks live site power for every Smart
Schedule charger type. Charging waits when telemetry is unavailable or there
is insufficient headroom for the charger's minimum current. Deadline charging
remains dynamically adjustable as household demand changes while preserving
the vehicle's maximum-current setting.

**Prioritize deadline charging over discretionary battery reservation**

Deadline sessions no longer reserve optimizer-planned battery charging capacity
in the initial, periodic or shared Tesla controller. This gives a departure
target priority over discretionary battery reservation while retaining the
site import ceiling.

Update available via HACS.
