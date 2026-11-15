# Advanced Powerwall controls

Paired Tesla V1R gateways offer advanced controls in the Home Assistant dashboard under **Controls → Advanced options**. PowerSync Cloud and mobile put the same controls under **Controls → More controls**. Availability depends on the gateway, firmware and pairing; a control is not offered as writable when its prerequisites are unavailable.

## Battery control

Choose **Fixed power**, **Idle**, **Charge to %** or **Discharge to %**. Fixed power uses battery watts: positive discharges, negative charges. Idle sends exactly zero watts. It does not mean maximum charging. The legacy Force Charge and Force Discharge controls retain their existing meaning and units.

Charge/discharge targets run at the chosen battery power until the requested percentage or maximum duration is reached. PowerSync supervises the percentage using fresh telemetry; the gateway command itself specifies power and a short lease. Percentages are whole values from 1 to 100. The existing maximum is 10,000 W in either direction, and duration is 1–1440 whole minutes. Requests outside the configured reserve or charge ceiling are refused rather than silently changed.

Stop ends the selected operation and returns control to Tesla. Loss of permission to start a new operation does not hide the owned Stop control. Leases renew only while telemetry, site limits, ownership and policy remain valid. On restart, PowerSync cleans up an interrupted dispatch rather than replaying its power request.

With V1R selected for optimiser control, a true idle slot uses the same bounded zero-power dispatch. Preserve-charge decisions remain separate: those decisions can allow solar charging while preventing discharge. Manual owners continue to take precedence over the optimiser.

## Site limits

**Site limits** contains import and export limits in watts. Enter either or both; blank leaves that direction unchanged. Zero is a hard limit. These are persistent gateway settings and remain after timed battery controls stop. The form requires no document reference, approval date or saved attestation.

PowerSync reads the current gateway settings, preserves other fields, applies the requested change and independently reads it back. Restore previous site limits restores the last owned snapshot only while the configuration still matches that change. A stale or uncertain change must be checked before another write. Re-pairing requires confirming the new gateway baseline. Legacy saved maxima remain restrictive until an explicit edit to that direction is confirmed.

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
