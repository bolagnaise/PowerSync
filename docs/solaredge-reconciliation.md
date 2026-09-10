# SolarEdge control reconciliation

PowerSync blocks further control after an uncertain SolarEdge write. Supervised reconciliation checks a fresh upstream poll before it clears that block. It does not write inverter registers or clear the block after a timeout.

## Mode rules

| Fresh storage mode | Required evidence | Result |
| --- | --- | --- |
| Native Maximize Self Consumption, code `1` | Recognised mode, successful fresh poll, matching inverter identity, no write in progress | Can clear the block without Remote Control fields |
| Remote Control, code `4` | Complete valid command, default command, timeout and power limits | Can clear the block only when both commands are benign and the saved baseline matches |
| Other or unknown modes | None accepted | Retains the block |

Native mode can report missing Remote Control fields or documented SunSpec not-implemented values. These fields do not prove active dispatch in native mode. The readback omits command mode, default mode, timeout and both power limits from its native snapshot, even when their values are readable.

Present malformed values still reject the snapshot. Remote Control requires every dispatch field, including the default command. The default command matters because it takes effect after the current command expires.

Timeout accepts integer seconds from `0` through `86400`. Power limits accept finite values from `0` through `1000000` watts. This limit follows the upstream control range; it does not represent the inverter's rated power. Backup reserve accepts values from `0` through `100` percent.

## Saved baseline policy

A fresh native self-consumption poll can supersede a saved Remote Control mode. Reconciliation ignores the saved remote command, default command, timeout and power limits in that case. Other applicable saved fields, including backup reserve and grid-charge policy, must still match. Unsupported saved control modes remain blocked.

Successful reconciliation clears the saved baseline and owned fields, then persists `ready`. It does not retain hidden values as verified settings. A failed save restores the previous in-memory safety record, including the pending mutation and generation counters.

A later forced operation must obtain fresh values for every field it will change or restore. PowerSync rejects the operation before writes when that complete baseline is unavailable. This change does not introduce an automatic transition from native mode into Remote Control. It cannot make forced dispatch available on hardware that hides the required controls.

With no baseline or owned fields, a fresh native poll makes `restore_normal` a successful read-only operation. The existing health, lock and generation checks still apply.

## Service results

The service still requires an explicit acknowledgement:

```yaml
action: power_sync.reconcile_solaredge_control
data:
  entry_id: <PowerSync config-entry ID>
  acknowledge: true
```

A successful response includes `success`, `control_health`, `reason` and `confirmation_source`. Native success reports `fresh_native_self_consumption_poll`; remote success reports `fresh_upstream_storage_poll`.

Clients that request response data receive failures as structured results with `success: false` and a reason code. Other callers receive a Home Assistant service error with that code. Field-specific results contain field names, without connection details or register values.

| Reason | Meaning |
| --- | --- |
| `readback_unavailable` | Upstream integration or supported readback contract is unavailable |
| `readback_not_fresh` | No successful newer poll with replacement decoded data completed |
| `identity_mismatch` | Entity, device and inverter identities do not agree |
| `controller_write_busy` | PowerSync already holds the inverter lock |
| `upstream_write_busy` | SolarEdge Modbus Multi reports a write in progress |
| `malformed_snapshot` | A required field is missing or a decoded value is invalid |
| `active_command` | Remote current or default command can dispatch battery power |
| `unsupported_storage_mode` | Fresh control mode is not explicitly supported for reconciliation |
| `baseline_mismatch` | Applicable saved fields differ from the fresh snapshot |
| `unresolved_active_power` | Storage registers cannot resolve the uncertain active-power write |
| `persistence_failed` | PowerSync could not save the reconciled state |

## Compatibility

The runtime contract was checked against SolarEdge Modbus Multi `3.3.9`. The adapter requires matching registry identities, a timestamp coordinator, and a replacement storage dictionary after a successful poll. It rechecks runtime and identity bindings after the poll. Registry changes cannot redirect a retained control journal to another inverter. Other versions must satisfy that same contract.

This is a register-based policy, not a model-specific exception. It applies to supported SolarEdge storage hardware that exposes the required register block. Some SolarEdge inverters do not expose battery controls over Modbus, even with an attached battery. Firmware and installation settings also affect availability.

Sources:

- [SolarEdge Modbus Multi storage polling](https://github.com/WillCodeForCats/solaredge-modbus-multi/blob/ca34a3fa9e888079097285e0487a4ee2de2753a0/custom_components/solaredge_modbus_multi/hub.py)
- [Register modes and sentinels](https://github.com/WillCodeForCats/solaredge-modbus-multi/blob/ca34a3fa9e888079097285e0487a4ee2de2753a0/custom_components/solaredge_modbus_multi/const.py)
- [Numeric control ranges](https://github.com/WillCodeForCats/solaredge-modbus-multi/blob/ca34a3fa9e888079097285e0487a4ee2de2753a0/custom_components/solaredge_modbus_multi/number.py)
- [Current and default command behaviour](https://github.com/WillCodeForCats/solaredge-modbus-multi/wiki/Storage-Control-Options)
- [Hardware limitations](https://github.com/WillCodeForCats/solaredge-modbus-multi/wiki/Known-Issues#not-all-inverters-support-battery-data-or-controls-over-modbus)

## Implementation and validation

Runtime changes:

- `inverters/solaredge_readback.py` validates mode-dependent fields and rechecks identity after the poll.
- `inverters/solaredge.py` reconciles applicable baseline fields, retires stale Remote Control baselines, and restores safety records after persistence failures.
- `coordinator.py` forwards detailed results and publishes control health.
- `__init__.py` returns structured results to response clients and reason-bearing errors to other callers.

Regression coverage is in `test_solaredge_readback.py`, `test_solaredge_controller.py`, `test_solaredge_reconciliation_integration.py`, `test_solaredge_runtime.py`, and `test_solaredge_service_responses.py` under `tests/`.

The integrated tests exercise the actual service, coordinator methods, controller and upstream readback together. They include a retained Remote Control baseline with an uncertain 5,000 W mutation and fresh native registers. They also test later forced requests, which must not reuse that baseline.

Validation on 2026-09-09 used Python 3.12.9 and pytest 9.1.1:

```text
python -m pytest tests/test_solaredge*.py -q
358 passed in 30.29s
```

`git diff --check` passed. Configured Ruff reported 819 existing findings across the changed legacy files, with no new findings against the starting commit. The new integrated test file passed Ruff.
