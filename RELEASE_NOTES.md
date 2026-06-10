<!-- release: v2.12.618 -->

## What's Changed

**Sigenergy native and VPP restore handoff**
Sigenergy restore now chooses the correct target for the current control mode. PowerSync Smart Optimization keeps Remote EMS enabled in self-consumption mode so scheduled dispatch can continue, while monitoring mode, native provider mode, disabled optimizer, or explicit restore handoff disables Remote EMS after restoring inverter limits so Sigenergy, Amber, or VPP control can resume. Monitoring-mode restores now allow that cleanup instead of blocking it, and automation actions use the same central restore path.

**Sigenergy EVDC solar handoff respects native control**
EVDC native solar handoff no longer re-enables Remote EMS when monitoring or native-provider mode is active, preventing PowerSync from pulling a system back under Remote EMS while users expect Sigenergy or VPP control.

**Enphase DPEL relay limits now cover all relay states**
Enphase dynamic export limiting now sends explicit percentage relay limits for every relay state. This helps newer AU firmware apply curtailment reliably and restores the relay configuration to 100 percent when limits are released.

**Mobile plan chart tooltips stay in view**
Optimization plan chart tooltips now move to the top of the chart when Android Home Assistant webview would otherwise render them outside the visible chart area.

Update available via HACS
