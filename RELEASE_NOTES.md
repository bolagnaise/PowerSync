## What's Changed

**Solax Hybrid battery system support**
PowerSync now supports Solax Hybrid inverters via the `wills106/homeassistant-solax-modbus` integration. Once the companion integration is installed and sensors are available, Solax systems are automatically detected and participate in LP optimization, force charge/discharge, and curtailment — no extra configuration required.

**Fix: Tesla Powerwall charging from grid in self-consumption mode**
When the optimizer transitions a Powerwall into self-consumption mode, it now explicitly resets the hardware backup reserve to the configured LP floor. Previously, if a force-discharge cycle had elevated the backup reserve (e.g. to 20%), the Powerwall would charge from the grid to meet that hardware floor even while in self-consumption mode — because backup reserve enforcement is independent of TOU mode. This was observed as unexpected grid charging at peak rates when the battery SOC was already well above the optimizer's floor.

**New: hardware mode drift detection**
On each optimizer cycle, PowerSync now reads the actual `default_real_mode` from Tesla's site_info API and compares it against what the optimizer believes. If the hardware mode has drifted away from self_consumption (e.g. due to Tesla firmware or a competing automation), a warning is logged and self-consumption mode is re-applied immediately. This makes mode drift visible in logs and self-healing without waiting for the next full transition.

**Fix: sensor.py crash on ESY Sunhome temperature sensor**
A missing `UnitOfTemperature` import caused the entire integration to fail to load whenever the ESY Sunhome battery system was configured. This has been fixed.

Update available via HACS
