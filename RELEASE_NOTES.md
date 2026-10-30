<!-- release: v2.12.1570 -->

## Exclude EV charger plans from battery optimization

Turning off **PowerSync EV charger plans** now also excludes Smart Schedule demand from the battery optimizer. Previously, that demand could still influence the battery plan after the option was disabled, including decisions to charge the home battery from the grid.

Update through HACS and restart Home Assistant. Find the option under **Configure → Smart Optimization → Battery & forecast inputs**. Your EV charging schedule continues separately; this fix does not guarantee that the battery optimizer will never choose grid charging for other reasons.
