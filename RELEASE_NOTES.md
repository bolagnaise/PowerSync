<!-- release: v2.12.1635 -->

## Amber cleanup after an optimiser charge

Fixes Native Max cleanup remaining on **Restoring Normal Operation** when the optimiser shortens a charge's stop time. The same session can now finish cleanup using a fresh Amber schedule, including recovery of an already affected session after restart.

Update through HACS and restart Home Assistant. Leave saved recovery files unchanged. Device ownership, settings confirmation and physical settling checks still apply.
