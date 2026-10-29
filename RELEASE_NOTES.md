<!-- release: v2.12.1543 -->

## What's Changed

### GoodWe status recovers after Home Assistant starts

PowerSync now retries when the GoodWe integration's sensors are still loading at startup, so status can recover without toggling Smart Optimization. Battery commands wait until GoodWe's readings and controls are ready.

Update through HACS and restart Home Assistant.
