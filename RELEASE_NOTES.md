<!-- release: v2.12.1518 -->

## Powerwall Grid Charging reliability

Manual Grid Charging changes and Tariff discharge now allow more time for Tesla's cloud setting to update, with one limited retry when Tesla still reports the opposite setting. Confirmation can take up to one minute across configured sites; Tariff discharge still requires confirmed Grid Charging Off.

## Recovery during discharge preparation

PowerSync now saves every site's original settings before waiting and retains restoration when preparation is cancelled or Home Assistant restarts. The optimiser rechecks the current plan, recent battery readings and reserve before proceeding.

Update through HACS and restart Home Assistant. This improves delayed-setting handling; a missing Tesla setting still cannot confirm that Grid Charging is Off.
