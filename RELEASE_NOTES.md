<!-- release: v2.12.1654 -->

## Restore Tesla charging after uncertain commands

Older Tesla commands with lost responses could leave Start and current control permanently blocked. Native Fleet, Teslemetry and Tessie charging now have a bounded recovery path that checks fresh vehicle state, preserves the original unknown outcomes and resumes normal command admission. Finite manual controls can reach that recovery before selecting their producer. Stop can interrupt recovery, and completed recovery survives Home Assistant restart.

For BLE, the new `power_sync.recover_ble_command_history` action covers Start, Stop, current and charge-limit history using the complete affected inventory and compatible companion firmware. Both Pedro and yoziru have matching adapters. A bridge commit interrupted by a restart stays recoverable without replaying old commands. Follow the updated firmware guide and preserve existing keys, pairing, VIN mappings and recovery files.

Recovery also checks the affected car's retained shared headroom reservation using fresh vehicle and meter evidence. Other cars' demand, electrical limits, Monitoring Mode and ownership checks remain in force. Recovery status separates historical unknowns from current blockers and shows retry/commit progress.

Update through HACS and restart Home Assistant. Native recovery requires the car to provide fresh stopped, zero-current and zero-power reports; BLE recovery additionally requires the documented companion firmware and explicit inventory action. Firmware source/build validation is separate from installation and live recovery.
