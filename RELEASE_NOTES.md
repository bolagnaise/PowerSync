<!-- release: v2.12.1516 -->

## What's Changed

**Powerwall Automatic charging with incomplete or slow local readings**
Automatic now retries an incomplete local telemetry response and uses the acquisition time of the reading that actually succeeded. It also refreshes local evidence after slow configuration work before submitting a command, avoiding preventable charging refusals while keeping the five-second freshness limit, SOC checks and site limits intact.

**Powerwall Local V1R grid charging and restoration**
When Smart Optimization permits grid charging, Local V1R now enables and verifies Tesla's charge-from-grid setting before requesting battery charging. PowerSync saves the original setting and restores it after charging or restart; repeated requests preserve that baseline, external disabling stops renewal, and uncertain writes or failed restoration retain recovery state with cleanup retries that do not restart charging. Manual V1R requests do not gain this optimiser permission.

Update available via HACS
