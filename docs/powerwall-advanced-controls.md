# Advanced Powerwall controls

Paired Tesla V1R gateways offer **Fixed power**, **Idle**, **Charge to %** and **Discharge to %** directly on the main **Controls** screen in Home Assistant, Cloud and mobile. Persistent site limits and backup preparation are under **More controls**. Tesla-managed / Max actions retain their separate meaning. Availability depends on the gateway, firmware and pairing; a control is not offered as writable when its prerequisites are unavailable.

## Manual controls and Monitoring Mode

Monitoring Mode prevents automatic battery actions. It does not prevent deliberate manual V1R controls, including fixed battery power, idle, percentage targets, persistent import/export limits and backup preparation. Existing manual settings such as reserve, operation mode, grid-charging permission, export preference, solar curtailment and calibration controls retain the same manual policy. Pairing, supported hardware, fresh readings, ownership and pending recovery still apply. Automatic actions and optimiser renewals remain blocked in Monitoring Mode.

## Battery control

Choose **Fixed power**, **Idle**, **Charge to %** or **Discharge to %**. Fixed power uses battery watts: positive discharges, negative charges. Idle sends exactly zero watts. It does not mean maximum charging. The legacy Force Charge and Force Discharge controls retain their existing meaning and units.

Charge/discharge targets run at the chosen battery power until the requested percentage or maximum duration is reached. PowerSync supervises the percentage using fresh telemetry; the gateway command itself specifies power and a short lease. Percentages are whole values from 1 to 100. The existing maximum is 10,000 W in either direction, and duration is 1–1440 whole minutes. Requests outside the configured reserve or charge ceiling are refused rather than silently changed.

Stop ends the selected operation and returns control to Tesla. Loss of permission to start a new operation does not hide the owned Stop control. Leases renew only while telemetry, site limits, ownership and policy remain valid. On restart, PowerSync cleans up an interrupted dispatch rather than replaying its power request.

With V1R selected for optimiser control, a true idle slot uses the same bounded zero-power dispatch. Preserve-charge decisions remain separate: those decisions can allow solar charging while preventing discharge. Manual owners continue to take precedence over the optimiser.

### Values, units and the whole-site total

| Setting | PowerSync input range | Meaning |
| --- | --- | --- |
| Fixed battery power | Whole watts from −10,000 to +10,000; the gateway can impose a lower limit | Total battery-system power for the controlled gateway, not watts per battery or phase. Negative charges, positive discharges, and zero holds idle. |
| Charge/discharge-to-% power | Positive whole watts from 1 to 10,000, subject to lower limits | The selected action sets the direction. A target must be on the requested side of the current battery percentage. |
| Duration / maximum duration | 1–1440 whole minutes in the app | PowerSync renews short device leases while the operation remains eligible. The underlying dispatch API uses 1–86,400 seconds. |
| Target battery level | 1–100 whole percent | Discharge must stay above the effective reserve; charging must stay within the charge ceiling. |
| Site import and export | Each accepts 0–1,000,000 whole watts | This application input ceiling is not an inverter rating or an approved connection allowance. Blank preserves that direction. Zero prohibits flow in that direction. |
| Backup duration | 1–1440 whole minutes | The native backup window has a fixed end time. |

Enter **5000** for **5 kW**, without commas or a unit suffix. Import and export are separate whole-site net ceilings at the gateway meter, across the site; do not divide them by the battery count or number of phases, and do not add the two ceilings together. The app uses watts while the native site-limit configuration uses kilowatts.

A power value within the input range can still be infeasible. The gateway's actual dispatch maximum can be below the combined inverter rating; it rejects an excessive request rather than silently reducing it. PowerSync also checks current solar, household load, reserve, permissions and connection limits. It does not guess an inverter rating from the number of batteries.

For example, with 3000 W household load and 1000 W solar, a +5000 W battery target would export 3000 W. A 2000 W export ceiling makes that exact target infeasible. Conversely, with 6000 W load, no solar and a 4000 W import ceiling, 1000 W discharge is too little: import would still be 5000 W. Even idle can be infeasible when house load or solar alone crosses a site ceiling. Setting a higher site ceiling does not increase inverter output capability, and lowering a ceiling cannot guarantee enforcement against loads or generation the battery cannot balance.

These values follow PowerSync's own API validation. Other integrations can expose different input ranges; those are not evidence that the same values work on this installation. See [Teslemetry's signed-dispatch reference](https://github.com/Teslemetry/tesla-fleet-api/blob/main/docs/fleet_api_energy_sites.md#energy-site-device-commands) and [aiopowerwall's dispatch and site-limit documentation](https://github.com/Teslemetry/aiopowerwall#api-surface). Acknowledgement, configuration readback and measured power remain distinct.

## Site limits

**Site limits** contains import and export limits in watts. Enter either or both; blank leaves that direction unchanged. Zero is a hard limit. These are persistent gateway settings and remain after timed battery controls stop. The form requires no document reference, approval date or saved attestation.

PowerSync reads the current gateway settings, preserves other fields, applies the requested change and independently reads it back. Restore previous site limits restores the last owned snapshot only while the configuration still matches that change. A stale or uncertain change must be checked before another write. Re-pairing requires confirming the new gateway baseline. Legacy saved maxima remain restrictive until an explicit edit to that direction is confirmed.

### Loading and a disabled Apply button

Mobile and Cloud load each advanced status independently and show loading or read errors. Background status updates keep typed values and confirmations in place. **Apply site limits** requires at least one valid whole-watt value and a current writable gateway configuration. A failed read, pairing change, active control owner or unresolved write can prevent a new change. The form explains the block; use **Refresh control state** after resolving it. Monitoring Mode alone is not a block for a manual edit.

## Backup preparation

Backup preparation schedules a native timed window to charge fully and retain energy for backup while remaining connected to the grid. It does not enable grid charging or widen site limits. Charging permission, enforced native connection limits and competing battery owners are checked before starting. A scheduled or manual backup conflict prevents acquisition.

End this backup window ends only the window owned by PowerSync. A replacement created elsewhere is left untouched. Check backup status reconciles an interrupted request using native readback; checking a healthy window leaves it running. Recovery never restarts or extends a backup window. The native window has its own fixed end time.

## Other V1R capabilities reviewed

| Capability | PowerSync use |
| --- | --- |
| Real-power dispatch, zero and resume | Advanced battery modes and V1R optimiser execution; implemented through owned short leases. |
| Import/export configuration | Persistent Site limits, consumed by optimiser and EV admission. |
| Native backup schedule/cancel/status | Backup preparation and existing HA Max Backup lifecycle. |
| Island/reconnect | Existing advanced off-grid and reconnect controls; backup ownership fences new islanding while allowing reconnection cleanup. |
| Calibration delay/status | Existing advanced calibration controls; retained separately from energy scheduling. |
| Solar curtailment and operating configuration | Existing provider-specific controls and optimiser paths; no new raw configuration editor. |
| Device/site information, network and connection status | Existing status and diagnostic surfaces. Useful for capability and freshness checks. |
| Device programs and OCPP configuration | Potential future charger/device scheduling work. Schema presence alone does not establish ownership, installed-device support or a verified owner-level contract. |
| Meter/phase detection, enumeration, provisioning, certificate setup and firmware triggers | Installer and maintenance functions, with no demonstrated benefit to ordinary energy scheduling. Evaluated, but not exposed as generic owner controls. |
| Inverter/protection/fan/vehicle-absence self-tests, reset, lockout clearing, constraint overrides and battery-disable controls | Commissioning or service operations. They require a dedicated documented workflow and verification, rather than being treated as optimiser actions. |
| Historical energy import | A database migration feature of the reference integration, rather than a V1R command; existing PowerSync history remains separate. |

Sources: [Teslemetry's force-control announcement](https://teslemetry.com/blog/powerwall-force-charge-and-discharge), [reference integration actions](https://github.com/cnapagoda/ha-powerwall-tedapi/blob/main/docs/actions.md), [pypowerwall](https://github.com/jasonacox/pypowerwall), and the vendored Tesla energy-device schemas in `powerwall_local/energy_device/proto/`. A declared protocol capability, accepted command, matching readback and measured physical effect are distinct forms of evidence. This change has automated source and protocol verification; it does not establish operation on every firmware or installation.
