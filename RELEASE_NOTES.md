<!-- release: v2.12.1810 -->

## Tesla charging recovery and tariff diagnostics

PowerSync now looks for two separate, recent readings of grid charging before it tries to restore normal Tesla operation during self-consumption. If Tesla accepts a tariff upload but its settings cannot be verified, debug logs identify which part of the tariff failed the check.

## Fronius status during negative import prices

Fronius status now shows the measured limit when PowerSync has temporarily stopped solar production at 0%. When the stop ends, simple-mode export control can resume using its saved settings and normal restore checks.

Update PowerSync through HACS and restart Home Assistant.
