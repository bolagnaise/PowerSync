<!-- release: v2.12.892 -->

# PowerSync v2.12.892

## Added

- **Tesla Powerwall local V1R control parity:** Added the remaining locally supported Teslemetry EnergySite commands to PowerSync. Paired Powerwalls can now manage grid charging policy, grid export rules, Go Off-Grid and grid reconnection, pairing verification, and Max Backup events directly over the LAN before using the Tesla cloud fallback.
- **Native Max Backup events:** Max Backup schedules now use the Powerwall's native local event API when available, survive Home Assistant restarts, and cancel the local event when the requested window ends. PowerSync retains the existing reserve-based fallback for installations that cannot use the native path.

## Improved

- **Immediate local readback:** Grid charging and export-rule entities now prefer fresh Powerwall telemetry and update immediately after successful local writes instead of waiting for cloud state to catch up.
- **Safer islanding and restoration:** Local Go Off-Grid and reconnect commands must be confirmed by fresh contactor telemetry. If the gateway acknowledges a command without changing grid state, PowerSync automatically falls back to the signed Tesla cloud command.
- **Fail-safe Max Backup fallback:** PowerSync refuses to raise reserve to 100% when it cannot establish a trustworthy value to restore afterward, preventing an interrupted local-only schedule from leaving the reserve permanently changed.

Update available via HACS
