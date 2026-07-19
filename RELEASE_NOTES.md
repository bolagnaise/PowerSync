<!-- release: v2.12.893 -->

# PowerSync v2.12.893

## Fixed

- **No Idle settings now apply immediately:** PowerSync now reconciles the saved Disable Idle setting with the running optimizer even when the saved value has not changed. Existing installations no longer need to enable, save, disable, and save again after an update before ordinary idle slots are converted to self-consumption.

## Improved

- **Safe live settings synchronization:** Repairing a stale runtime No Idle state now triggers a fresh optimization without leaving a stale config-entry reload guard behind. Intentional Charge By Time deadline holds remain unchanged.

Update available via HACS
