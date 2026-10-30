# Powerwall calibration deferral

Paired Tesla systems expose a whole-hour calibration-delay action and a separate gateway status read. This requests a deferral; it does not cancel calibration, force a discharge, or clear PowerSync's calibration alert or optimiser guard.

The controls offer 1, 2, 3, 6, 12 and 24 hours. Home Assistant actions also accept other positive whole hours within the protocol's unsigned 32-bit range. Firmware may reject a requested duration. Before sending, PowerSync requires a fresh typed status response with an available deferral allowance. A missing or unimplemented status disables the selector; it does not establish that the delay command itself is unsupported.

## Home Assistant

- `power_sync.powerwall_get_battery_calibration_delay_status` returns the gateway's remaining deferrals, maximum deferrals and delay remaining in seconds.
- `power_sync.powerwall_delay_battery_calibration` accepts `delay_hours`, optional `entry_id` and optional `request_id`. Specify the entry when multiple entries exist. Reuse the same request ID when checking an uncertain request; never generate a new ID automatically to retry it.
- The action's hour selector is available in Developer Tools → Actions. PowerSync Mobile exposes it on Tesla controls for HA connections.
- Authenticated mobile endpoint: `GET`/`POST /api/power_sync/powerwall/calibration_delay`; POST requires integer `delay_hours` and a unique ASCII `request_id`.
- Monitoring Mode permits status reads. Automated writes are blocked while Monitoring Mode is enabled; monitoring-only battery connections always block writes.

## Cloud and mobile

Paired Tesla sites expose **Calibration delay** on Controls. Select hours, inspect the reported allowance, then explicitly press **Delay calibration**. **Refresh status** is read-only. Cloud uses the signed Tesla relay and needs internet; HA uses the paired gateway on the LAN.

`GET /v1/dashboard/:siteId/calibration-delay` reads status. `POST` accepts `{ "delay_hours": 1, "request_id": "unique-intent-id" }`. The command shares Force ownership and pairing checks; stop an active Force operation and allow its restoration to finish first. Calibration deferral is intentionally not scheduled or automatically retried because a request may consume a finite gateway allowance.

A typed acknowledgement is shown separately from gateway status. Acknowledgement alone does not prove that a calibration was deferred. There is no Fleet/tariff fallback or documented cancel command. Durable request records prevent replay of uncertain commands, and short signature fences prevent a later calibration-delay request from overtaking an uncertain signed request. Cloud also fences subsequent Force acquisitions. HA retains uncertain records and fails closed if its bounded journal is full.

## Evidence and firmware limits

When Tesla reports a local `BatteryCalibration` alert, PowerSync marks **Calibration Active** and pauses automatic battery charge/export control. Missing alert data does not confirm that calibration has ended: the pause remains until three consecutive valid alert readings no longer contain `BatteryCalibration`. A gap in the alert data restarts that confirmation count. This does not add a new calibration-detection signal or infer calibration from an SOC jump.

The native requests are TEG fields 75/77, with typed responses 76/78. Delay is positive integer hours; status reports seconds. Interface details were checked against [Teslemetry aiopowerwall PR 28](https://github.com/Teslemetry/aiopowerwall/pull/28).

[Brett's hardware results in PR 29](https://github.com/Teslemetry/aiopowerwall/pull/29) report that a one-hour delay was acknowledged on PW3 firmware 26.26.4, but its effect could not be verified because the status getter returned `UNIMPLEMENTED`. The owner's read-only test also returned gateway status code 12 (`UNIMPLEMENTED`), so PowerSync disables the delay action on that gateway. No calibration-delay write was sent during PowerSync verification. No physical deferral or allowance reset behavior has been proven.
