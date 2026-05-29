<!-- release: v2.12.503 -->

## What's Changed

**Scale the dashboard Force Power control to the system**
The Force Power slider now uses the configured optimizer limits, inverter-specific rated limits, and live BMS power ceilings when available instead of always presenting a 0-50 kW range. This makes manual force charge and force discharge easier to use on smaller systems such as 15 kW inverters, while still falling back to 50 kW when no better limit is known.

**Restore Self Consumption in Tesla dashboard controls**
The generated Home Assistant dashboard now detects Tesla operation-mode entities through the same suffix-aware resolver used by the controls card, including newer `power_sync_tesla_*` entity IDs. This brings the Operation Mode tile back for affected dashboards so Self Consumption can be selected from the dashboard again.

Update available via HACS
