<!-- release: v2.12.1571 -->

## Powerwall recovery after an unsuccessful charge or discharge

PowerSync can now restore normal settings after an optimizer-requested Powerwall charge or discharge fails partway through setup. Previously, cleanup could stall, leaving the force mode active with an expired countdown and preventing the optimizer from refreshing its plan.

Update through HACS and restart Home Assistant. Tesla settings that still cannot be confirmed remain pending for recovery; this fix does not bypass those checks.
