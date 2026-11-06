<!-- release: v2.12.1642 -->

## Tesla force-discharge settings moved to Smart Optimization

Tesla Powerwall force-discharge buy and sell prices are now under **Smart Optimization > Dispatch behaviour**, alongside the battery dispatch controls, instead of Tesla API connection. Your existing saved values are preserved.

These temporary prices tell Tesla to discharge; they are separate from your actual tariffs and the optimiser's decision thresholds. Changes apply to the next force-discharge session. Force-charge pricing remains unchanged.

Update through HACS and restart Home Assistant.
