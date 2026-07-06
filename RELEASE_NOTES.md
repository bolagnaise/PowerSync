<!-- release: v2.12.777 -->

## What's Changed

**Prevent duplicate Sungrow Modbus polling from AC inverter curtailment**
PowerSync now blocks AC inverter curtailment from being configured against the same Sungrow Modbus endpoint as the battery system. Existing same-endpoint configurations also skip the separate AC inverter status poller after updating, preventing a second Modbus client from polling the SH hybrid inverter alongside the main battery coordinator.

**Clarify AC-coupled inverter setup**
The setup text now describes this option as a separate AC-coupled or string inverter path and warns hybrid-only Sungrow users not to point it at the battery inverter already configured as the PowerSync battery system.

Update available via HACS
