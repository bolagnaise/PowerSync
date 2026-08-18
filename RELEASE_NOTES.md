<!-- release: v2.12.1135 -->

## What's Changed

**Steady EV charging no longer makes Sungrow Home Load disappear**
PowerSync now timestamps charger-power measurements from Home Assistant's own latest source report, rather than only from the last time the numeric value changed. A steady 11 kW BLE charging reading therefore remains current and is subtracted exactly once from Sungrow's gross site load; the reported 13.77 kW site balance now produces 2.77 kW Home instead of an unavailable value that downstream displays could show as zero. Fleet, Wall Connector, app-managed, and HACS OCPP power entities use the same per-field timestamp rule.

**Missing Home telemetry remains fail-closed**
Connection, location, charging-state, and ownership metadata still cannot freshen measured EV power. If a power source genuinely stops reporting, PowerSync preserves Home Load as unavailable through the automation adapter, direct solar-surplus control returns no safe surplus, and the standard Smart Schedule planner waits instead of treating the missing value as measured zero. Grid-based surplus control continues to use its independent grid, battery, and EV measurements.

Update available via HACS
