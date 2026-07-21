<!-- release: v2.12.903 -->

## What's Changed

**Clearer settings navigation**
PowerSync's main settings menu now keeps the everyday battery, pricing, optimization, and EV choices up front while moving specialist export, curtailment, weather, maintenance, and cloud-flow options into a dedicated Advanced settings menu.

**Smart Optimization settings in four focused sections**
Smart Optimization controls are now grouped into Core goals, Behaviour, Battery & limits, and Advanced optimizer controls, making reserve, charge-by-time, grid-charge, battery specification, and site-limit settings easier to find without presenting one oversized form.

**Safe saves across Home Assistant and mobile clients**
Each section refreshes the live configuration before saving and applies only the fields actually submitted. This prevents a stale hidden value from overwriting a reserve or optimizer setting that changed elsewhere while the form was open.

**Versioned settings metadata for companion clients**
The optimization API now advertises a versioned field schema while retaining the existing settings groups for compatibility. Companion clients can safely expose supported controls such as grid charging, charge-price and SOC caps, and explicit site export limits without guessing backend capabilities.

Update available via HACS
