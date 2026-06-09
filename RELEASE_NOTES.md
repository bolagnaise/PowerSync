<!-- release: v2.12.614 -->

## What's Changed

**Anker Solix battery support**
PowerSync now supports Anker Solix X1/Solarbank systems through direct local Modbus TCP, the official local Anker Home Assistant integration, or the unofficial Anker cloud Home Assistant bridge. Direct Modbus and writable HA bridge paths can provide telemetry, Smart Optimization, force charge/discharge, restore normal, battery health, and force-power controls; telemetry-only cloud setups are detected and reported as monitoring-only when write entities are unavailable.

**EV charger state and manual stop handling**
Generic EV charger setup now accepts an optional measured charging-power sensor. PowerSync uses that power reading to show actual charger power, infer connected/charging state more accurately, and avoid treating commanded amps as active charging when the charger is not drawing power. Manual stop actions also create a short hold so price-level charging does not immediately restart a vehicle the user just stopped.

**Force-mode controls are more flexible and robust**
Manual force charge/discharge duration options now include the missing 15-minute steps between 2 and 4 hours, matching the mobile control range. Tesla force charge/discharge now retries Time-Based Control mode writes and verifies the readback before continuing, reducing cases where a transient Tesla API response leaves a force command half-applied.

**Flow Power KWatch double-encoded responses**
Flow Power KWatch API parsing now unwraps JSON responses that arrive as JSON strings before extracting sites and price records. This fixes API-key validation and KWatch pricing for accounts where the endpoints return HTTP 200 but double-encode the response body.

Update available via HACS
