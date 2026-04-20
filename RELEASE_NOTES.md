## What's Changed

**Fix: Force Charge "User" Override After HA Restart**
When HA restarts while a force charge is active and the charge expires before startup completes, the optimizer could incorrectly treat it as a user-initiated force charge rather than an optimizer-initiated one — causing it to defer instead of taking over. This happened because the `source` field wasn't copied when populating the cleanup state. The LP now correctly recognises expired optimizer force charges and resumes control on the first run after restart.

Update available via HACS
