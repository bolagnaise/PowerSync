<!-- release: v2.12.1508 -->

### Native Sungrow iHomeManager battery control

A new **iHomeManager battery control + inverter telemetry** connection supports automatic and manual battery charging, discharging and return to self-consumption. It uses the native iHomeManager endpoint for commands and a separate Sungrow SH inverter connection for live telemetry. Setup checks both connections using reads only and requires native protocol V1.0.2.

Update through HACS and restart Home Assistant, then choose the new connection under **Configure > Sungrow Modbus connection**. Supply both endpoints; existing monitoring-only connections stay unchanged. The new connection starts in Monitoring Mode. Verify telemetry and disable Sungrow AI/VPP or other battery controllers before enabling PowerSync control.

Native Idle holds, reserve changes, spread export, solar curtailment and AC22E-01 charger control are not supported by this connection. Software tests have passed; physical operation on the reported SH20T/SBH400 system has not yet been verified. Battery discharge does not guarantee grid export.
