<!-- release: v2.12.1120 -->

## What's Changed

**Tesla deadline charging no longer cancels a valid slow cloud start**
PowerSync now allows up to 150 seconds for Tesla charging-state and measured-draw telemetry to settle after an accepted Smart Schedule start. This covers cloud-provider transitions that arrive just beyond the previous 90-second cutoff, preventing a valid charge-by-time start from being immediately cancelled and retried too late to reach its target.

**Physical-start verification remains fail-closed**
An accepted command still does not count as charging on its own. PowerSync continues to require fresh, exact-vehicle charging state plus measured current or power; if that proof does not arrive within the extended window, it sends the compensating stop and creates no timer, session, or ownership.

Update available via HACS
