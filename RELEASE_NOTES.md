<!-- release: v2.12.1521 -->

## Fixed

### Powerwall Tariff recovery when Tesla omits a setting

Returning to **Tariff** after failed Local V1R setup no longer stops before attempting restoration when Tesla’s cloud response omits the current grid-charging permission. PowerSync can make one attempt to restore the saved **Grid Charging Off** setting after verifying the gateway and completing local resume.

The missing field does not count as success: cleanup remains pending until Tesla explicitly confirms the restored setting. Update through HACS, restart Home Assistant, then retry selecting **Tariff**. This corrects a remaining recovery gap in v2.12.1520; it does not establish the cause of the original local charging failure.
