# EV state-of-charge sources

## Design and implementation plan

An AC OCPP charger can report connection, current and power without knowing the
car's battery percentage. PowerSync should use an explicitly configured Home
Assistant EV SoC sensor for that car without requiring generic charger control.
The existing primary and fallback fields provide that configuration, so no
second pair of OCPP settings or replacement sensor entity is needed.

The [HACS OCPP sensor implementation](https://github.com/lbbrhzn/ocpp/blob/main/custom_components/ocpp/sensor.py)
exposes the `SoC` measurand as a battery sensor when available. It scopes
multi-connector measurements to their connector. Charger power, delivered energy
and a vehicle's battery percentage are separate measurements; PowerSync does not
calculate a battery percentage from charger energy.

| Source | Resolution |
| --- | --- |
| Generic single-car charger | Existing configured primary, then fallback sensor |
| Generic shared charger | Manually selected vehicle profile's sensor |
| OCPP | Exact connector's native SoC, then configured primary/fallback for one unambiguous OCPP charger |
| Tesla Fleet/BLE | Existing vehicle identity and provider-specific sensors |
| Sigenergy DC | Existing native vehicle SoC |
| Zaptec | No new association with a global EV sensor; explicit vehicle association remains necessary |

Implementation steps:

1. Extend the shared sensor reader to resolve OCPP sources while preserving
   generic charger settings and profile selection.
2. Use it for OCPP status, canonical loadpoint display, Smart Schedule, Price
   Level and dynamic charging's target-SoC lookup.
3. Reject unavailable, non-numeric, non-finite, out-of-range and explicitly
   non-percentage readings. Preserve a measured zero.
4. Keep native readings scoped to their exact charger. Use global fallback only
   when one OCPP charger exists and a separate generic charger/profile is not
   configured. Count offline chargers too. Never use unrelated Tesla readings
   or cached estimates for an OCPP request.
5. Clear Smart Schedule plans and report unavailable SoC when the source cannot
   be resolved; request a stop if that schedule is already charging. Retain
   existing ownership and command-confirmation safeguards.
6. Verify source precedence, invalid readings, multiple chargers, unavailable
   sources, schedule planning and API projection before the full release gate.

The primary and fallback sensors must describe the **same car**. They are
alternative sources, not automatic vehicle selection. Sensor updates and
availability remain the responsibility of their Home Assistant integration;
PowerSync does not wake the vehicle or refresh its hardware to read SoC.

## Platform compatibility

Home Assistant owns these sensor entities and all affected lookup paths. Its
existing optional loadpoint `soc` and OCPP `current_soc` fields carry the result.
Mobile already consumes the canonical HA loadpoint field, including unavailable
values, so no mobile release or new payload contract is required. PowerSync
Cloud has a separate OCPP backend and no access to these local HA entity IDs;
this HA sensor-setting change does not require a Cloud deployment.

Verification uses synthetic HA entity and charger fixtures. Optimizer-only
fleet replay inputs do not establish OCPP vehicle-sensor association; physical
charger behavior requires a separate reporter retest after installation.
