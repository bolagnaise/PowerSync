<!-- release: v2.12.894 -->

## What's Changed

**Timed manual EV charging now survives Home Assistant restarts**
Finite manual EV charging sessions now persist their absolute stop deadline and restore the timer without sending another start command. Sessions that expired while Home Assistant was offline are stopped during recovery when the charger is still on, while chargers already off are left alone.

**Tesla force control now uses paired local Powerwall access**
Force charge, force discharge, and restore now apply operation mode and backup reserve through Powerwall Local V1R first, with Fleet API fallback for unpaired or additional gateways. Failed or unconfirmed writes keep cleanup armed, retry on a bounded backoff, and restore the saved tariff, reserve, mode, and grid-charging policy instead of trusting a full-duration force window.

Update available via HACS
