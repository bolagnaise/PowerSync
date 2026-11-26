<!-- release: v2.12.1851 -->

## EV charging can use the Powerwall calibration discharge

During a Tesla calibration, the Powerwall discharges on its own and anything the home does not use is exported. Solar Surplus never counted battery discharge as available, and its battery floors paused the EV, so the calibration energy was exported even when the feed-in price was low.

While Tesla reports a calibration, Solar Surplus EV charging can now charge your EV from that discharge whenever exporting is not worth more than your Solar Surplus export limit. The EV only takes energy that is already being exported, so the car is never charged from the grid this way. Normal battery floors return once the calibration ends.

## Clearer Powerwall calibration notification

The calibration notification now explains that Tesla discharges and then recharges the battery and ignores charge and export commands until it finishes. It also points to Delay Powerwall Calibration in the Tesla app if you want to move it. Update through HACS and restart Home Assistant.
