<!-- release: v2.12.1581 -->

## Tesla restoration after cancelled battery commands

Fixed PowerSync staying on “Restoring normal operation” and holding up Home Assistant startup when a tariff-driven charge or discharge is cancelled during preparation. Cleanup can now restore the saved settings while keeping other battery commands safely ordered.

Update through HACS and restart Home Assistant. Settings that cannot be confirmed still remain pending for recovery; do not delete saved recovery state.
