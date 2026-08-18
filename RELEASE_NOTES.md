<!-- release: v2.12.1139 -->

## What's Changed

**Fronius curtailment now distinguishes command delivery from physical effect**
Fronius load-following control no longer treats a successful Modbus write response as completed curtailment. PowerSync now serializes control and status traffic for the same inverter endpoint, reads back the SunSpec limit-enable, limit-percentage, and reversion controls, and rejects a command or restore when the device settings do not match the requested values.

The inverter status sensor and API now expose the requested target, confirmed device limit, residual site export, and physical convergence separately. While export remains above the existing 100 W threshold, the state reports **Load Following Pending** instead of claiming that physical curtailment has completed. The confirmed Fronius limit percentage is also retained in status rather than being replaced with an unavailable value.

**Persistent export is rechecked even when the target has not changed**
The 30-second load-following loop now reapplies a verified Fronius limit when the calculated watt target still matches the cached request but fresh site telemetry continues to show material export. Once the site reaches the threshold, PowerSync records convergence without an unnecessary write. This preserves existing price, battery, monitoring, and control-ownership gates and does not change Amber or inverter configuration.

Update available via HACS
