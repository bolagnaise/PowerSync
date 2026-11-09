# Fronius simple-mode curtailment after restart

PowerSync retains a restore record before it disables SunSpec throttling in
Fronius simple mode. After a Home Assistant restart, it checks that record against
the configured endpoint, the inverter's SunSpec serial number and fresh control
registers. A matching curtailment is shown as pending until physical convergence
is verified. Restoration still follows the existing price, battery, monitoring
and manual-control rules; startup recovery itself sends no control command.

This covers changes made from normal output (SunSpec limiting enabled at 100%).
Disabled throttling by itself does not prove that PowerSync owns the setting.
Pre-upgrade states without a restore record, changed devices, and user power caps
are therefore not automatically adopted. Failed restores retain the record for
retry. Neither deleting the record nor an unconditional startup restore is a safe
recovery procedure.

The record uses the SunSpec Model 1 serial layout, documented in the
[SunSpec common model](https://github.com/sunspec/models/blob/master/json/model_1.json).
An unavailable serial or mismatching readback defers recovery.

Low SOC alone does not override an active limit while export remains uneconomic.
Fresh battery charging with headroom remains an exception. Negative import prices
retain priority. To investigate a different outcome, capture the same-time prices,
battery power and SOC, grid power, inverter status attributes, and untruncated
PowerSync debug records around the transition.
