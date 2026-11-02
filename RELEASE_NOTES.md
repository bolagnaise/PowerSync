<!-- release: v2.12.1607 -->

## Clearer battery status in monitoring mode

The dashboard's Right now card now follows measured battery power in monitoring mode, so it no longer says the battery is powering the home while it is charging. The plan's intended action is shown separately, such as "The plan would charge now.", and upcoming changes read "Next it would…".

This change applies to monitoring mode, where PowerSync plans without controlling the battery. Update through HACS, restart Home Assistant, and refresh the dashboard.
