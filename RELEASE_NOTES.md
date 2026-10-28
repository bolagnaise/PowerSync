<!-- release: v2.12.1520 -->

## Fixed

### Returning to Tariff after failed Powerwall Local V1R charging

If Local V1R charging fails while checking Tesla’s grid-charging permission, selecting **Tariff** can now recover the saved permission through verified Tesla cloud access when local restoration is unavailable. PowerSync confirms the same gateway, waits for outstanding local writes, and restores the original setting before allowing the switch.

Cleanup stays pending if resume or restoration cannot be confirmed. The error now distinguishes an unfinished settings restore from an unconfirmed resume. Update through HACS, restart Home Assistant, then retry selecting **Tariff**; this correction does not establish why the original local write failed.
