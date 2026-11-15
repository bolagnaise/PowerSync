# Services Reference

PowerSync services are battery-agnostic where possible. Brand-specific control is routed through the configured coordinator, so most automations should use the generic `power_sync.*` services instead of brand names.

## Core Services

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.sync_tou_schedule` | Manually refresh Time-of-Use tariff prices. Tesla systems upload the schedule; Modbus systems store it for PowerSync display and optimization | None |
| `power_sync.sync_now` | Refresh provider and battery data immediately | None |

## Battery Control

### Tesla on-grid solar curtailment — advanced

Use **Settings → Devices & services → PowerSync → Configure → Advanced settings → Tesla on-grid solar curtailment (export-limit warning)**. The PowerSync dashboard no longer exposes this switch.

**Warning: disabling on-grid solar curtailment bypasses the site export restriction.** Solar can export above the configured limit even though that limit remains saved. Leave enabled to enforce the site export restriction. The permission persists across Home Assistant restarts; updating PowerSync does not change its current value.

The advanced action `power_sync.powerwall_set_on_grid_solar_curtailment` takes required boolean `enabled` and optional `entry_id` (required for multiple entries). It requires local pairing and LAN access and confirms the gateway setting by readback. It is separate from price-based curtailment and does not control an independent AC-coupled inverter.

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.force_charge` | Force battery charge from grid for a timed window | `duration` optional, minutes, default 30; `power_w` optional, 0/omitted uses the inverter default |
| `power_sync.force_discharge` | Force battery discharge/export for a timed window | `duration` optional, minutes, default 30; `power_w` optional, 0/omitted uses the inverter default |
| `power_sync.hold_battery_soc` | Hold the battery around its current SOC and block normal charge/discharge where the hardware supports it | `duration` optional, minutes, default 30 |
| `power_sync.set_self_consumption` | Put the battery into self-consumption mode | None |
| `power_sync.restore_normal` | Cancel force/hold/self-consumption overrides and return to normal operation | None |

### Custom charge/discharge timing

In Battery controls, enable **Custom charge/discharge time** to enter hours and minutes under **Run for**, or a finish time under **Until**. Both Charge and Discharge use this timing; +/− changes duration by one minute. Quick presets return to preset timing. Hold charge and Power the home continue to use the Run for preset. Tap twice to confirm; changing time or power requires confirmation again.

Both services accept whole-minute `duration` values from 1 to 1440, or `until: "18:30"` in Home Assistant’s configured timezone. Supply one timing field only; omitting both retains 30 minutes. Until selects the next valid occurrence, shows tomorrow when appropriate, and rounds up by less than one minute. DST skips/repeated hours use the next real occurrence; windows beyond 24 elapsed hours are rejected. Provider limits still apply, including Amber’s 240-minute maximum, FoxESS’s 1092-minute maximum and native SolaX/Neovolt duration support. Existing discharge Custom entity IDs and saved values remain compatible.

**Coverage notes:**

- Force charge/discharge routes through Tesla, Sigenergy, Sungrow, FoxESS, GoodWe, AlphaESS, Solax Hybrid, ESY Sunhome, SAJ H2/HS2, Fronius Reserva, SolarEdge, and Anker Solix where the configured hardware exposes the required controls.
- On Tesla, a manual battery command waits for an in-flight optimizer command to finish before taking control. A successful request or active countdown confirms command handling; use fresh Battery Power and Grid Power readings to verify discharge/export.
- Sigenergy manual Force Discharge and optimizer export use PV-first discharge whenever live DC or third-party AC solar is present, even when solar is below the grid-export ceiling. ESS-first is selected only when live solar is known to be zero. The configured grid-export ceiling and ESS discharge limits still apply; PV-first prioritizes solar but does not guarantee a particular battery discharge rate.
- Neovolt / Bytewatt is also supported when the upstream Neovolt integration exposes dispatch mode, power, duration, charge target, and discharge cutoff entities.
- Anker Solix direct X1 Modbus and the official local HA bridge support dispatch when third-party control/write entities are available. The unofficial Anker cloud bridge can be telemetry-only when owner-account or writable preset entities are missing.
- Custom / external controller systems are planner-only. These services do not dispatch hardware for that battery system; use the optimizer sensors as inputs to your own controller or automations.
- `power_w` is best-effort. ESY Sunhome is mode-only, and SAJ H2/HS2 uses a percentage of rated power through the upstream SAJ integration.
- `hold_battery_soc` is strongest on Modbus systems with a standby/backup mode. Tesla and AlphaESS are best-effort; solar surplus can still move SOC.
- Force charge/discharge state is persisted across Home Assistant restarts. If a timed force mode is still valid after restart, PowerSync restores the mode and its expiry timer; if it expired during downtime, PowerSync restores normal operation. SolarEdge is an exception: startup does not replay persisted force commands or timers. Retained or uncertain controls require [supervised reconciliation](SolarEdge-Control-Recovery).

## SolarEdge Control Recovery

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.reconcile_solaredge_control` | Read fresh storage settings and clear the persistent control block only when a safe baseline is verified; writes only to restore the baseline of a dispatch PowerSync itself completed | `entry_id` required; `acknowledge: true` required |

Inspect the inverter under supervision before calling this service. A restart or a successful service request alone does not establish safe device settings. See [SolarEdge Control Recovery](SolarEdge-Control-Recovery) for the procedure and the limitation for uncertain active-power curtailment writes.

## Backup Reserve

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.set_backup_reserve` | Set the hardware backup reserve / minimum SOC | `percent` required, 0-100 |

Supported reserve writes: Tesla Powerwall, Sigenergy, Sungrow, FoxESS, GoodWe, Solax Hybrid, Fronius Reserva, SolarEdge, and Neovolt / Bytewatt when the upstream reserve/cutoff entity exists. ESY Sunhome, SAJ H2/HS2, and direct Anker X1 Modbus do not expose a validated PowerSync backup reserve write path, so the call is logged as unsupported or no-op for those systems.

Tesla currently rejects reserve values from 81-99%; PowerSync clamps those requests to 100%.

From v2.12.1320, Tesla backup reserve changes, including optimizer and restore requests, use the Tesla cloud API and require a separate matching reserve readback. Local pairing does not provide offline reserve control. If cloud access or confirmation fails, PowerSync reports the reserve change as unconfirmed and retains its existing restore/retry safeguards. Other local telemetry and controls remain available.

PowerSync no longer guesses the user-facing reserve from local gateway configuration or treats a pending local write as a measured reserve. The Backup Reserve entity uses the cloud value, with its existing saved-value fallback when cloud data is unavailable; local raw reserve remains diagnostic data. Previously saved user preferences are not automatically rewritten. Confirm your intended reserve after updating if you previously saw unexpected settings.

## Tesla Powerwall Settings

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.set_operation_mode` | Set Powerwall operation mode | `mode`: `autonomous`, `self_consumption`, or `backup` |
| `power_sync.set_grid_export` | Set Powerwall grid export rule | `rule`: `never`, `pv_only`, or `battery_ok` |
| `power_sync.set_grid_export_auto` | Restore Tesla's grid export rule from PowerSync's preferred automatic behavior | None |
| `power_sync.set_grid_charging` | Enable or disable grid charging | `enabled` boolean |
| `power_sync.set_storm_watch` | Enable or disable Storm Watch when the site supports it | `enabled` boolean |
| `power_sync.set_off_grid_ev_reserve` | Set the battery percentage reserved for EV charging during an outage | `percent` required, 0-100 |
| `power_sync.set_vpp_enrollment` | Enroll or unenroll from a Tesla grid-services program | `program_id`, `enrolled` boolean |
| `power_sync.schedule_max_backup` | Raise backup reserve to 100% for a fixed window, then restore the previous reserve | `duration_minutes` required, 1-1440 |
| `power_sync.refresh_calibration` | Clear PowerSync's local Powerwall calibration guard after a calibration cycle | None |
| `power_sync.resume_powerwall_settings` | Resume cloud settings after an unresolved local settings write; retains the incident and original restore evidence | `entry_id`, `site_id`, `incident_id` from Repairs; explicit `acknowledge: true` |

The upcoming local-settings update adds supervised recovery under **Settings > System > Repairs** when a signed local reply is lost. Run **Resume Powerwall settings through cloud** using the IDs in the notice, then use **Restore Normal** or make the intended setting change and verify Tesla settings and measured Battery/Grid Power. Resumption itself issues no device command. It persists across restart and failed cloud attempts; local settings writes remain suspended. The old full configuration write may still apply later, so this action accepts uncertainty rather than proving completion. Original restore evidence remains with the incident after active forcing ends. Ignoring the notice does not resume control.

Before starting Tesla force charge or force discharge, PowerSync records the current Grid Charging setting so normal operation can restore it exactly. If neither paired local control nor Tesla's cloud response exposes that setting and PowerSync has no remembered preference, the force command is rejected before any Powerwall setting changes. Set the **Grid Charging** control in PowerSync once, then retry; the confirmed preference is remembered for later force sessions.

From v2.12.1338, Tesla setup and **Settings → Tesla API connection** include temporary **Force-discharge buy price** and **Force-discharge sell price** fields. They are prefilled at $0.55/kWh and $25.00/kWh. The sell price must exceed the buy price. These values are Tesla control signals during the force-discharge window, not your retail import or feed-in tariff; PowerSync restores the saved tariff afterward. Changing the fields affects the next force-discharge session. Tesla may still choose not to export, so confirm the uploaded tariff and live battery/grid power before concluding that discharge occurred.

## Tesla Powerwall Local Control

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.powerwall_go_off_grid` | Physically disconnect from grid using local Powerwall control | `bypass_soc_check` optional boolean; `mode` optional advanced TEDAPI mode override |
| `power_sync.powerwall_reconnect_grid` | Reconnect to grid | None |
| `power_sync.powerwall_verify_pairing` | Verify the stored local pairing key | None |

Local off-grid control requires completed gateway pairing. Powerwall 3 uses signed TEDAPI commands; Powerwall 2 uses the local gateway path.

See [Battery System Setup](Battery-System-Setup#powerwall-local-control-pairing) for the full pairing process, including the 2-minute DC-isolator toggle window and re-pair confirmation behavior.

### Powerwall Local Control Entities

When a Tesla site is configured, PowerSync also exposes helper entities for local control:

| Entity | Purpose |
|--------|---------|
| Pair Powerwall Gateway button | Starts local Powerwall gateway pairing. Re-pairing requires a second press within 30 seconds. |
| Unpair Powerwall Gateway button | Clears the stored local key after a two-press confirmation. |
| `switch.power_sync_off_grid` | Calls local off-grid / reconnect services and reflects the actual island state when local data is available. |
| Powerwall Local Paired binary sensor | Shows whether local pairing is complete |
| Powerwall Gateway IP Missing binary sensor | Flags Tesla sites that need a local gateway IP before local controls can work |
| Powerwall Off-Grid binary sensor | Mirrors the local island/on-grid state when available |

## AC Inverter Curtailment

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.curtail_inverter` | Manually curtail the configured AC-coupled inverter | `mode` optional: `load_following` default or `shutdown` |
| `power_sync.restore_inverter` | Restore the configured AC-coupled inverter to normal operation | None |

Automatic curtailment uses the configured AC inverter settings and current price/load data. Manual `load_following` mode passes home load to controllers that support it; unsupported paths fall back to zero export or native curtailment behavior.

## Data Services

| Service | Description | Parameters |
|---------|-------------|------------|
| `power_sync.get_calendar_history` | Return battery energy history for the mobile app | `period` optional: `day`, `week`, `month`, or `year` |
| `power_sync.sync_battery_health` | Update Tesla battery-health data from a mobile TEDAPI scan or capacity blueprint | `original_capacity_wh`, `current_capacity_wh`, `degradation_percent`; optional `battery_count`, `scanned_at` |

`get_calendar_history` supports service responses in Home Assistant versions that expose response data from service calls.

## Control Entities

These are Home Assistant entities, not Developer Tools services, but they are part of PowerSync's control surface:

| Entity | Description |
|--------|-------------|
| `switch.power_sync_auto_sync` | Automatically refreshes tariff prices. Tesla sites show this as Auto-Sync TOU Schedule because the schedule is uploaded to the Powerwall; non-Tesla systems show Auto-Sync Tariff Prices because PowerSync stores the prices for display and optimization |
| `select.power_sync_force_charge_duration` | Default duration used by force-charge controls |
| `select.power_sync_force_discharge_duration` | Default duration used by force-discharge controls |
| `number.power_sync_force_power_kw` | Preferred manual force charge/discharge power target in kW; 0 uses inverter/BMS maximum |
| `switch.power_sync_optimization_enabled` | Turns Smart Optimization on or off without deleting saved optimizer settings |
| `switch.power_sync_monitoring_mode` | Blocks battery and inverter write commands while still allowing prices, sensors, forecasts, and plans to update |
| `switch.power_sync_auto_update` | Enables PowerSync's HACS update automation; configure the update time in **Configure > PowerSync auto-update** |
| `switch.power_sync_away_mode` | Makes the load forecaster recover from holiday/low-load periods |
| `switch.power_sync_profit_max_mode` | Uses the more aggressive LP terminal value profile for strong export windows |
| `switch.power_sync_optimization_spread_export` | Smooths planned battery export across eligible export windows when supported |
| `switch.power_sync_optimization_spread_import` | Smooths planned grid charging across same-price import windows when supported |

Tesla sites also expose number/select/switch/binary entities for backup reserve, off-grid EV reserve, operation mode, grid export rule, grid charging, Storm Watch, VPP program enrollment, manual export override, grid-services status, permission-to-operate, active Powerwall alerts, and calibration state when the site supports them.

## PowerSync Automation Actions

The mobile app's Automations engine also exposes actions such as Enable Optimizer, Disable Optimizer, AEMO spike export, EV charging actions, and grid-status workflows. These are PowerSync automation actions, not separate Home Assistant Developer Tools services.

The **Import kWh** and **Export kWh** triggers measure cumulative site grid
energy in the selected direction inside a required daily time window. Each
fires once when its configured kWh threshold is crossed, persists its window
state across restarts, and resets for the next local window. Import uses only
point-of-common-coupling import and Export uses only point-of-common-coupling
export; unavailable counters fall back to bounded live grid-power integration
instead of treating missing telemetry as zero. Use the existing IF conditions
and actions to apply the measured threshold safely. Export kWh is exposed only
when the Home Assistant backend advertises support; Cloud automations do not
accept this trigger.

## Tesla battery output and native site limits

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
