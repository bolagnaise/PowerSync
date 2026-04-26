## What's Changed

**AlphaESS: Solar curtailment now works correctly when force charge/discharge is active**
On Smile firmware, the active dispatch register (0x0880) overrides the export-limit register (0x0800), so any in-progress force charge or discharge would silently prevent curtailment from taking effect — the register write would succeed but the inverter would ignore it. Curtailment now releases the active dispatch first before writing the export limit, so solar curtailment works regardless of what mode the optimizer put the inverter in.

**AlphaESS: DC Curtailment option added to setup**
`CONF_ALPHAESS_DC_CURTAILMENT_ENABLED` was already used internally but had no UI — every AlphaESS user was running with curtailment silently disabled regardless of their intent. The toggle is now visible in the AlphaESS Modbus setup step. It includes a warning that the AlphaESS firmware must have Modbus export-limit control enabled first (an inverter setting, not a PowerSync config) — without it the register write succeeds but the inverter ignores it physically.

Update available via HACS
