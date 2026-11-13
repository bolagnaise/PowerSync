# Battery settings and manual controls

- [Manual charge and discharge timing](#manual-battery-timing)
- [Minimum battery export price and Solar Export](#minimum-battery-export-price)
- [Powerwall calibration deferral](#powerwall-calibration-delay)
- [Storm Watch notifications](#tesla-storm-watch-notifications)

<a id="manual-battery-timing"></a>

## Manual charge and discharge timing

In **Battery controls**, enable **Custom charge/discharge time**. Choose **Run for** and type the hours and minutes, or choose **Until** and enter a finish time such as `18:30`. Both **Charge** and **Discharge** use this timing. The duration + and − buttons change it by one minute. Tap your action twice to confirm; changing timing or power requires confirmation again.

Quick duration buttons return Charge and Discharge to preset timing. **Hold charge** and **Power the home** continue to use the preset Run for duration. Custom duration values initialize from the existing discharge Custom selection; existing entity IDs and saved discharge settings remain compatible.

Until uses the timezone configured in Home Assistant, even when your browser uses another timezone. The preview says today or tomorrow. It selects the next real occurrence of that time: a skipped daylight-saving time is skipped, and a repeated time uses its next occurrence. The remaining duration rounds up to whole minutes, so the timer can finish less than one minute after the entered time. A next occurrence beyond 24 elapsed hours is rejected.

### Service calls

Both `power_sync.force_charge` and `power_sync.force_discharge` accept `duration` as a whole number of minutes from 1 to 1440, or `until` as an `HH:mm` string. Supply only one timing field. Omitting both retains the 30-minute default.

```yaml
action: power_sync.force_charge
data:
  duration: 77
```

```yaml
action: power_sync.force_discharge
data:
  until: "18:30"
```

Existing `duration: Custom` discharge automations still resolve the saved custom duration. The same value is accepted for charge. Provider restrictions still apply: Amber overrides support at most 240 minutes; FoxESS supports at most 1092 minutes; SolaX and Neovolt must support the requested native duration. SolaX native charge schedules cannot represent an exact 24-hour window. Unsupported requests fail before taking ownership or changing an existing timer.

The classic dashboard provides an editable custom duration and finish-at controls too. A request or countdown confirms command handling; fresh battery/grid telemetry is needed to establish the physical result. Safety limits, power limits, and restoration rules still apply.

<a id="minimum-battery-export-price"></a>

## Minimum battery export price and Solar Export


### Behavior

Set **Minimum battery export price** in PowerSync's Optimization options to
the lowest real feed-in price at which the optimizer may intentionally export
stored battery energy. The UI and API use c/kWh; persisted configuration uses
currency units/kWh. A value of 30 means 30c/kWh, with equality permitted.
Zero disables the additional floor and preserves existing tariff policies.
Other reserve, economic, site-limit, ownership, and monitoring checks still apply.

This setting does not prevent the battery supplying household loads or natural
solar export. It is an optimizer policy, not a physical zero-export guarantee
or a restriction on user-owned manual force controls or external controllers.

For solar diversion below the battery floor, enable Profit Maximisation and
disable Chip Mode. Chip Mode independently suppresses below-threshold solar
prices. Profit Max Solar Export also requires a supported charge-hold adapter,
a finite positive export limit, and sufficient cheaper reachable replenishment.
Charge By Time requires that replenishment to fit before its deadline.

Example: with a 30c battery floor, 9c solar now and 3c solar later, PowerSync can
hold battery charging and export the current solar surplus, then release the
hold and charge from cheaper later solar. Stored battery export below 30c is
blocked. A shrinking solar forecast or deadline can remove the hold. Prices
above the floor permit battery export but do not force it.

### Diagnostic status

The Current Action sensor (`sensor.power_sync_optimization_status`) and API
include `battery_export_price_policy`, containing the configured minimum,
evaluated price in c/kWh, whether that price passes the floor, and its reason.
`disabled` means this additional floor is off; passing it does not imply that
all other export checks passed or that an inverter accepted a command.
The separate `profit_max_solar_export` section explains solar eligibility and
hold state. Prices are evaluated from the coordinator's available tariff
snapshot, not independent live hardware telemetry.

<a id="powerwall-calibration-delay"></a>

## Powerwall calibration deferral

Paired Tesla systems expose a whole-hour calibration-delay action and a separate gateway status read. This requests a deferral; it does not cancel calibration, force a discharge, or clear PowerSync's calibration alert or optimiser guard.

The controls offer 1, 2, 3, 6, 12 and 24 hours. Home Assistant actions also accept other positive whole hours within the protocol's unsigned 32-bit range. Firmware may reject a requested duration. Before sending, PowerSync requires a fresh typed status response with an available deferral allowance. A missing or unimplemented status disables the selector; it does not establish that the delay command itself is unsupported.

### Home Assistant

- `power_sync.powerwall_get_battery_calibration_delay_status` returns the gateway's remaining deferrals, maximum deferrals and delay remaining in seconds.
- `power_sync.powerwall_delay_battery_calibration` accepts `delay_hours`, optional `entry_id` and optional `request_id`. Specify the entry when multiple entries exist. Reuse the same request ID when checking an uncertain request; never generate a new ID automatically to retry it.
- The action's hour selector is available in Developer Tools → Actions. PowerSync Mobile exposes it on Tesla controls for HA connections.
- Authenticated mobile endpoint: `GET`/`POST /api/power_sync/powerwall/calibration_delay`; POST requires integer `delay_hours` and a unique ASCII `request_id`.
- Monitoring Mode permits status reads. Automated writes are blocked while Monitoring Mode is enabled; monitoring-only battery connections always block writes.

### Cloud and mobile

Paired Tesla sites expose **Calibration delay** on Controls. Select hours, inspect the reported allowance, then explicitly press **Delay calibration**. **Refresh status** is read-only. Cloud uses the signed Tesla relay and needs internet; HA uses the paired gateway on the LAN.

`GET /v1/dashboard/:siteId/calibration-delay` reads status. `POST` accepts `{ "delay_hours": 1, "request_id": "unique-intent-id" }`. The command shares Force ownership and pairing checks; stop an active Force operation and allow its restoration to finish first. Calibration deferral is intentionally not scheduled or automatically retried because a request may consume a finite gateway allowance.

A typed acknowledgement is shown separately from gateway status. Acknowledgement alone does not prove that a calibration was deferred. There is no Fleet/tariff fallback or documented cancel command. Durable request records prevent replay of uncertain commands, and short signature fences prevent a later calibration-delay request from overtaking an uncertain signed request. Cloud also fences subsequent Force acquisitions. HA retains uncertain records and fails closed if its bounded journal is full.

### Evidence and firmware limits

When Tesla reports a local `BatteryCalibration` alert, PowerSync marks **Calibration Active** and pauses automatic battery charge/export control. Missing alert data does not confirm that calibration has ended: the pause remains until three consecutive valid alert readings no longer contain `BatteryCalibration`. A gap in the alert data restarts that confirmation count. This does not add a new calibration-detection signal or infer calibration from an SOC jump.

The native requests are TEG fields 75/77, with typed responses 76/78. Delay is positive integer hours; status reports seconds. Interface details were checked against [Teslemetry aiopowerwall PR 28](https://github.com/Teslemetry/aiopowerwall/pull/28).

[Brett's hardware results in PR 29](https://github.com/Teslemetry/aiopowerwall/pull/29) report that a one-hour delay was acknowledged on PW3 firmware 26.26.4, but its effect could not be verified because the status getter returned `UNIMPLEMENTED`. The owner's read-only test also returned gateway status code 12 (`UNIMPLEMENTED`), so PowerSync disables the delay action on that gateway. No calibration-delay write was sent during PowerSync verification. No physical deferral or allowance reset behavior has been proven.

<a id="tesla-storm-watch-notifications"></a>

## Storm Watch notifications

When Tesla reports that Storm Watch is active, PowerSync creates a Home Assistant
notification and sends a push to registered PowerSync mobile devices. The message
explains that storm protection may charge the Powerwall from the grid or pause
normal battery discharge.

This uses Tesla's current Fleet/Teslemetry status. Turning on the Storm Watch
setting alone does not send an alert. Local gateway readings without cloud Storm
Watch status cannot establish activation.

PowerSync alerts once when activation is observed, including when the first
reading after Home Assistant starts is already active. Repeated readings and
integration reloads do not repeat the notification. Missing or invalid readings
do not clear or rearm it. When Tesla explicitly reports protection inactive,
PowerSync dismisses the Home Assistant notification and can alert on the next
activation. A Home Assistant restart can alert again if protection is still active.

The notification reports Tesla's status. It does not change Storm Watch, battery
settings or the optimiser plan, and does not confirm battery power flow. Check
Battery Power and Grid Power for measured charging or discharging.

Update through HACS and restart Home Assistant to receive these notifications.
