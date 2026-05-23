<!-- release: v2.12.460 -->

## What's Changed

**Hardware backup reserve in Smart Optimization setup**
Smart Optimization settings now include the battery hardware backup reserve directly under the optimizer minimum discharge level. This makes the outage reserve visible in the same flow, keeps it separate from the optimizer floor, and gives PowerSync the correct restore target after temporary hold or force-control modes.

**Planned export power for supported batteries**
Supported target-power batteries now use the optimizer's planned export wattage instead of always falling back to maximum discharge power. SAJ H2 TOU discharge control now converts the requested export power into the correct slot percentage, so export windows can be throttled to the planned level instead of always running at 100%.

**Solar surplus charger fallback**
Solar surplus EV charging now falls back to the configured entry charger when no app vehicle configs have been saved yet. This keeps single OCPP, Generic, Sigenergy, and Zaptec charger setups available to solar surplus logic without requiring a separate vehicle config record first.

Update available via HACS
