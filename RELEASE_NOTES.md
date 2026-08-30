<!-- release: v2.12.1212 -->

## What's Changed

**Plan with a finite backup or V2X energy allowance**
Smart Optimization can now include a configurable daily backup/V2X energy allowance, maximum power, and local availability window. The allowance offsets forecast native-home grid import only, is tracked across rolling optimizations so it is not granted twice, and remains planning-only: PowerSync does not command a vehicle, charger, transfer switch, or generator.

**Set a global minimum battery export price**
A new minimum export price setting prevents planned, bridged, committed, and live battery-export actions below the configured real settlement price. The default of 0 c/kWh preserves existing tariff bonus and saving-session behavior.

**Optionally keep solar curtailment active in Monitoring Mode**
Solar curtailment can now be explicitly allowed while Monitoring Mode continues to block unrelated battery and EV controls. The existing configurable curtailment threshold and release deadband remain in effect, and external-controller handoff fencing is still enforced.

Update available via HACS
