<!-- release: v2.12.1223 -->

## What's Changed

**GoodWe entity-control status now distinguishes acknowledgement from physical effect**
When a GoodWe EMS command is sent through Home Assistant entities, PowerSync now reports that the command was acknowledged by the entity surface while physical battery discharge remains unverified. This prevents an optimistic entity echo from being presented as proof that the battery is discharging, while preserving the manual override so the optimiser does not fight the user's command.

Normal-operation restore messages on the same entity-control path now use the same distinction. Direct GoodWe control continues to report its existing confirmed control status.

Update available via HACS
