# Tesla advanced control services

These separate controls require a paired Tesla gateway and compatible signed-control firmware. Read current status before submitting a revision-bound request. Specify `entry_id` whenever more than one PowerSync entry exists.

| Service | Purpose | Required inputs |
|---|---|---|
| `power_sync.dispatch_battery_power` | Request a fixed battery output for a timed window | `power_w`, `duration_seconds`, `request_id`, `expected_control_revision` |
| `power_sync.stop_battery_dispatch` | Stop the specific operation returned by the start request | `operation_id` |
| `power_sync.set_native_site_limits` | Set persistent limits or manage their approval/recovery state | Operation-specific inputs below |

Battery output uses integer watts: positive discharges, negative charges. Zero hold is unavailable. Duration is an upper bound; infeasible requests are refused and infeasible renewals stop. Very short windows can be refused because a signed request must expire safely inside its window. `takeover: true` replaces another battery target only; stop legacy Force controls separately first. A successful command receipt does not verify physical tracking.

For native limits, supply nonnegative integer `import_limit_w` and/or `export_limit_w`, a unique `request_id`, and current `expected_config_revision` and `expected_approval_revision`. Omitted directions remain unchanged, and zero is an explicit limit. A native setting persists beyond Stop and restart. PowerSync preserves unrelated gateway settings and requires independent readback; an uncertain write blocks further acquisition until reconciliation.

`operation: approve` requires a Home Assistant administrator, approved maxima, `confirm_installation_limits: true`, a `document_reference`, an `approval_date` and the current approval revision. This records software authority and sends no hardware setting. Raising limits requires current approval; a saved baseline alone grants no permission. `operation: restore` needs an owned `snapshot_id` and current revisions and must remain within current approval. No unlimited/reset behavior is assumed.

Use `operation: reconcile` after an uncertain write; it reads the gateway after the acceptance window and requires two matching configurations. After replacing or re-pairing a gateway, an administrator can use `operation: rebaseline` with `confirm_pairing_change: true` and current configuration/approval revisions to archive old authority and record fresh limits without applying a setting. Approval must then be established again.

Authenticated status is available from `/api/power_sync/powerwall/battery_dispatch` and `/api/power_sync/powerwall/native_site_limits`, with `entry_id` as a query parameter. The dashboard reads these automatically. See [Smart Optimization](Smart-Optimization) for planner, managed EV and external-controller boundaries.
