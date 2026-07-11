<!-- release: v2.12.823 -->

## What's Changed

**Fix Powerwall Hold SoC reserve restore**
Manual Hold SoC on Tesla Powerwall now records the pre-hold backup reserve and treats an active hold as restorable state when the timer expires or Restore Normal is pressed. This prevents a Powerwall 2/local-paired setup from staying parked at the elevated hold reserve after the app countdown clears.

**Use the local Powerwall path for hold rollback**
Hold SoC reserve rollback now routes through the same local-first backup reserve service used by normal reserve changes, with Fleet API as fallback. Local-paired Powerwalls no longer need cloud-only site config access for the reserve to return to the user's saved value.

Update available via HACS
