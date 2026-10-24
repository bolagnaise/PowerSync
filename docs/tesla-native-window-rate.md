# Rate management inside Tesla charge windows

Available in the Home Assistant integration from v2.12.1482.

PowerSync can optionally adjust a Tesla vehicle's charging current while the car is already charging at home in a recurring Tesla schedule with explicit start and end times. Tesla continues to start and stop charging and enforce its charge limit. PowerSync does not wake the car, edit its schedule, change its charge limit, or pause a Tesla-owned session.

**Plan Tesla charge schedules** remains a forecast setting, enabled by default. The separate **Adjust rate in Tesla windows** setting grants current-control permission for that vehicle and defaults off. Turning forecast inclusion on does not enable rate control.

## Requirements and limits

- A paired local Powerwall with fresh whole-site measurements and a known site import limit, plus an enabled PowerSync optimiser.
- A loaded Tesla Fleet or Teslemetry vehicle integration supplies current vehicle data and the saved schedule. Existing supported Tesla BLE, Teslemetry Bluetooth or Fleet current entities supply the actual amp command.
- A unique vehicle/provider mapping, fresh electrical metadata, and an explicit recurring home window. One-time, start-only and finish-by-only schedules remain forecast-only.
- Grid-charging permission, gateway limits and other controller ownership still apply. PowerSync keeps the original positive current setting as its maximum; this permission does not authorize exceeding that setting.

Current is calculated from the remaining vehicle energy and time before the window ends. Battery charging, home load, other EV demand and pending commands share the site budget. If the minimum positive charging current does not fit, PowerSync cannot stop a Tesla-owned session; its status reports the limitation. A target can remain unreachable because of site capacity, vehicle limits or a short window.

The first implementation supports Tesla vehicle current control. Native rate management is withheld where PowerSync has configured another charger controller, or existing managed charging demand has not been reconciled. Existing controllers continue operating while the new feature is unavailable. A pending native current restoration must finish before an unsupported controller can take over.

## Battery and forecast coordination

Before increasing EV demand into capacity currently used by a local grid-following battery charge, PowerSync records the prospective EV demand, lowers that exact battery charging session, and waits for two fresh site readings showing sufficient headroom. It rechecks authority and measurements before sending the EV command. Manual battery charging also respects reserved EV demand; a legacy tariff charge cannot enforce this shared wattage budget.

An eligible active native window uses one fixed EV forecast derived from the same current, remaining-energy and deadline calculation as rate control. Existing native scheduling forecasts remain in use elsewhere. Losing authority, recovery or a manual takeover removes the controlled forecast and requests a new household plan. Forecasts and acknowledged current settings are separate from measured charging power.

Keep enforced gateway connection limits configured. Software coordination uses sampled measurements and asynchronous provider commands; it is not a replacement for electrical protection or the gateway's enforced limits.

## Restoration and manual changes

Disabling rate management, leaving a supported window, stopping the optimiser, or restarting Home Assistant ends active control. PowerSync restores only the original current it owns and checks available import headroom before increasing it. An offline vehicle, uncertain command or insufficient capacity can leave restoration pending; the journal survives restart and does not replay a charging target.

After the original current setting is acknowledged, its prospective demand stays reserved until two fresh, aligned vehicle and site readings confirm physical settlement. Battery charging cannot consume that headroom while the car ramps up. Waiting for settlement does not resend the current command.

A manual current change or another controller taking ownership supersedes PowerSync. PowerSync does not overwrite that change or reacquire the same native window. Native Charge on Solar and native-window rate management are mutually exclusive across PowerSync entries for the same vehicle.

## Home Assistant API

Authenticated `GET /api/power_sync/ev/tesla_schedule_planning?vin=<VIN>` returns each loaded entry's forecast setting, rate permission, availability reason and current status. `POST` accepts `entry_id`, `vin`, `enabled`, and the optional explicit boolean `rate_management`. A rate permission requires forecast inclusion. These settings requests do not themselves establish physical charging proof.

The mobile control appears under **EVs & Chargers > Vehicles** with a matching app build (1.0.7 build 454 or later). **Refresh rate status** reads PowerSync's local status without waking or polling the vehicle.

This document describes the Home Assistant implementation. Cloud rate control and mobile build delivery require separate implementation and validation; their publication must not be inferred from an HA source change.
