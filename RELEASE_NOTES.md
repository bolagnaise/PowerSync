<!-- release: v2.12.553 -->

## What's Changed

**Sigenergy curtailment now corrects stale cached state**
PowerSync now re-applies Sigenergy zero-export curtailment when live grid telemetry shows export is still happening, even if the cached curtailment state says the inverter is already curtailed. This fixes a case where Sigenergy systems could continue exporting during low or negative feed-in prices after another control path restored the export limit.

**Sigenergy curtailment gets periodic reinforcement**
While export earnings remain below the curtailment threshold, PowerSync now tracks the last Sigenergy curtailment write and can periodically resend the zero-export command. Restoring normal export clears that marker so the next curtailment window starts from a clean state.

Update available via HACS
