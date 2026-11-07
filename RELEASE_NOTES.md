<!-- release: v2.12.1651 -->

## More accurate battery energy and charging costs

Measured energy and cost tracking now follows fresh telemetry between optimizer runs, including paired local Powerwall readings when available. Previously, a change between charging and discharging could assign the preceding five minutes to the final power reading, undercount charging costs and unnecessarily increase unknown stored energy.

Update through HACS and restart Home Assistant. Tracking accuracy still depends on the available telemetry; existing unknown energy is kept conservative and is not reassigned by this update.
