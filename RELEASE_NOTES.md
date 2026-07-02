<!-- release: v2.12.750 -->

## What's Changed

**Fix EV load subtraction for monitoring-only generic chargers**
PowerSync now includes the configured generic charger power sensor when subtracting EV charging from historical home-load forecasts, even when generic charger control is disabled and the charger is used for monitoring only. This fixes inflated household load forecasts for setups that monitor a charger power sensor, such as a Tesla Wall Connector, without enabling Smart Schedule charger control.

Update available via HACS
