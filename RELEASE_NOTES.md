<!-- release: v2.12.1727 -->

## Clearer Powerwall site-limit status

When Powerwall site-limit controls are unavailable, the Home Assistant dashboard now identifies the specific control, connection restriction, or pending recovery blocking the change. Approved support diagnostics include the same runtime check without polling the gateway or changing settings.

Update through HACS and restart Home Assistant. This update improves diagnosis; it does not clear recovery holds or automatically enable site-limit writes.
