# SolarEdge control recovery

PowerSync stops SolarEdge control writes when an entity service call may have reached the inverter but its result cannot be confirmed. A failed call does not establish that the inverter rejected the command or returned to normal operation. PowerSync preserves the prior baseline and records the uncertain write. It does not attempt rollback, zero the charge or discharge limits, or retry the command.

The control block survives Home Assistant restart. Startup does not replay persisted SolarEdge force commands or expiry timers. Enabling monitoring remains available while control is blocked and does not attempt inverter cleanup. A controller lock serializes battery and curtailment operations; automatic requests skip a busy controller. Expiry timers carry the controller generation so they cannot restore a later command.

The Battery Integration Details sensor exposes `control_health`, `control_mutation_active`, and `last_control_mutation`. A value of `reconciliation_required` means that further control requests remain blocked. This state does not say that battery power is zero or that the last command stopped.

## Supervised recovery

Inspect the inverter and its upstream SolarEdge Modbus Multi connection. Establish safe storage settings using the inverter's supported controls under supervision. Keep automatic dispatch disabled during this inspection.

Then call `power_sync.reconcile_solaredge_control` with the PowerSync configuration entry ID and `acknowledge: true`:

```yaml
action: power_sync.reconcile_solaredge_control
data:
  entry_id: YOUR_POWERSYNC_ENTRY_ID
  acknowledge: true
```

This service reads storage settings through a fresh update of the existing upstream integration. It does not send inverter writes. It clears the block only when it identifies the correct inverter, obtains fresh readback, and validates a benign storage baseline. When PowerSync retained a prior baseline, every saved field must match that baseline. Cached Home Assistant entity values, elapsed time, and a restart are insufficient. Unsupported upstream readback or an unsafe baseline leaves the block in place.

A successful reconciliation clears the retained transaction and allows the next command to capture its baseline. Review the control-health attributes before resuming automatic dispatch. Reconciliation does not restore an earlier force command or timer.

Storage readback cannot validate an uncertain active-power curtailment write. The reconciliation service leaves that block in place; recovery requires separate validation of the active-power controls.
