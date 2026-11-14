# Electricity providers

- [Amber Smart Shift](#amber-smartshift)
- [Flow Power Happy Hour Plus](#flow-power-happy-hour-plus)
- [AGL Battery Rewards](#agl-battery-rewards)
- [Octopus Energy and BottlecapDave](#octopus-energy)

<a id="agl-battery-rewards"></a>

## AGL Battery Rewards

The evening export reward changes the feed-in rate while retaining your configured
import prices. A peak import period continues through the reward window even when
the tariff also has an all-day off-peak fallback.

After updating through HACS, restart Home Assistant. PowerSync repairs unchanged
generated reward schedules using their saved import tariff and reward settings.
Hand-edited schedules are preserved; review and save those through the tariff
editor if their reward periods overlap incorrectly.

<a id="amber-smartshift"></a>

## Amber Smart Shift

PowerSync connects directly to Amber Smart Shift from the Home Assistant integration. No separate Amber battery integration, blueprint installation, or external controller is required. Your battery must already be enrolled in Smart Shift and working in the Amber app.

### Setup

Choose Amber Electric as the electricity provider and configure your public Amber API token and pricing site as usual. For the battery system, choose **Amber Smart Shift — native cloud battery control**, then **PowerSync native Amber Smart Shift** as the connection profile. Enter the credentials used by the Amber app. The discovered battery must belong to the selected pricing site.

The connection form exposes these settings:

| Setting | Meaning |
| --- | --- |
| Amber email | Your Amber account email. |
| Amber password | Your Amber account password. Leave blank when editing an existing connection to retain it. Passwords are never prefilled. |
| Let PowerSync manage battery operation | Off by default. Leave off for Amber-managed operation and manual PowerSync controls; enable to allow PowerSync optimisation and automation commands. |
| Allow PowerSync to enable Smart Shift for overrides | Off by default. When allowed, an override may enable Smart Shift and leave it enabled. Without permission, requests are blocked if Smart Shift is off. |
| Grid power (positive import) | A fresh W or kW sensor, positive for import and negative for export. Required for automated control. |
| Solar power | A fresh W or kW solar-production sensor. Required for automated control. |
| Home load power | A fresh W or kW home-load sensor. Required for automated control. |

Change these settings through **Configure → Amber Smart Shift connection and control owner**. Keep the battery capacity and rated charging/discharging power in PowerSync's optimisation settings accurate. Amber selects actual battery power; those ratings inform the plan and software safety checks.

### Operation

The PowerSync dashboard provides Charge, Export, Preserve, Self-consumption, and Cancel PowerSync override controls. Dashboard actions last 30 minutes; services accept durations from 1 to 240 minutes. Existing `power_sync.force_charge`, `force_discharge`, `hold_battery_soc`, `set_self_consumption`, and `restore_normal` services use this backend when selected. The entry-targeted `power_sync.preserve_battery` service provides a timed charge-preservation override.

Include the PowerSync `entry_id` in service calls. Explicit manual requests use `source: manual`; unattended automations use `source: automation` and require PowerSync management to be enabled. Target watts are unsupported: omit `power_w` or set it to zero. A nonzero explicit target is rejected.

PowerSync maps charge, export, preserve, and self-consumption to Amber's timed battery modes. Preserve holds charge without changing a persistent hardware reserve. Restoring normal operation cancels only the override PowerSync owns. An override from the Amber app remains external; cancel it in Amber before requesting a PowerSync override. Automatic PowerSync actions cannot replace or cancel a manual override.

Automatic overrides have a maximum ten-minute lease. The optimiser revisits the current action and its safety gates before renewing a lease near expiry; a stale plan cannot renew. Shorter reserve-safe durations remain shorter. When an override expires, normal Amber management resumes, including if Home Assistant is offline. Timed manual requests retain their requested duration.

Turning the Smart Shift switch off cancels PowerSync's own override and disables PowerSync-managed operation. Turning it on does not enable PowerSync management; choose that separately in configuration.

### Status and limitations

Battery SOC, battery power, capacity, Amber-reported activity, connectivity, last poll time, control owner, active override, expiry and command status appear as native PowerSync entities. Battery telemetry is polled every 30 seconds. Missing or stale measurements remain unavailable. Selected grid, solar and home-load meters must report within 90 seconds for automatic commands.

The dashboard distinguishes a requested action, API acknowledgement, and Amber's reported override. **Reported** means Amber returned the matching override ID and mode. It does not prove the battery physically charged or exported; check battery power, SOC and grid flow for that evidence. Planned watts are estimates.

Future or incompletely timed overrides appear as pending. Unavailable battery telemetry produces unknown current status instead of displaying the optimiser's planned action as an active mode.

Interval import cost, export earnings and net earnings use Amber's current interval metrics in cents. They are not settled billing totals and do not replace PowerSync's public Amber pricing, forecasts or metered-usage sensors.

This backend cannot set target watts, persistent hardware reserves, off-grid operation, or solar-production limits. It cannot enforce a dynamic network export-power envelope; forced export is blocked when one is active. Solar curtailment requires a separately supported inverter control path.

SOC reserve and charge limits are checked in software, including during active overrides. They are not hardware-enforced guarantees during an outage. Amber's own hardware protection remains separate. Automatic writes also require matching pricing/battery identity, current interval prices, fresh telemetry, and the existing monitoring, grid-charge, demand, export and ownership gates.

### Troubleshooting

- **Switching back to Tesla:** choose **Configure → Change battery / control method → Tesla**, complete the Tesla API connection, then select your **Tesla energy site** when prompted. Switching away removes the previous Tesla site selection. If Tesla is already selected but setup reports a missing energy site, open **Configure → Tesla API connection** to select it again. Smart Shift mode control does not provide Powerwall solar curtailment or Tesla's Grid Export setting.
- **Sign-in failed:** check the Amber credentials and complete any sign-in challenge in Amber. Unsupported challenges are rejected; PowerSync does not bypass them. Reconnect through HA's reauthentication prompt.
- **Site/configuration mismatch:** select the matching pricing site and reconnect. PowerSync does not guess which battery to control.
- **Command blocked:** check control-owner permission, Monitoring Mode, meter freshness, prices, SOC limits and Smart Shift status. A plan can remain visible while execution is blocked.
- **Unconfirmed command:** the API response or override readback is incomplete. PowerSync retains its journal and blocks replacement until reconciliation or expiry, rather than blindly repeating the write.
- **Cleanup pending:** PowerSync retains the exact owned override ID for retry. Check Amber connectivity and expiry. An override created later in the Amber app is never cancelled as cleanup for an older PowerSync command.

Amber battery control uses a private API. Changes or outages may temporarily make it unavailable. Price-only Amber setups and existing direct battery backends continue to use their existing paths.

<a id="flow-power-happy-hour-plus"></a>

## Flow Power Happy Hour Plus

Select **Happy Hour Plus (40c)** in the Flow Power plan selector during setup or
in the integration's Flow Power options when your written contract matches this
offer:

| Local time | Export credit |
| --- | --- |
| 17:30–21:30, first 15 kWh each day | 40c/kWh |
| 17:30–21:30, additional exports | 10c/kWh |
| Outside the window | 0c/kWh |

This preset matches the supplied Happy Hour Plus offer. It is a separate choice
from Battery Happy Hour; existing selections retain their rates. If your terms
differ, use Account-specific contract and its custom export tiers instead.

The 15 kWh allowance covers measured grid exports in the window, including solar
and battery exports. It resets on the next tariff day in Home Assistant's tariff
timezone. Planned exports do not consume the measured allowance.

The current price sensor shows 40c/kWh while measured allowance remains, then
10c/kWh when it is exhausted. When today's quota cannot be established from
telemetry, PowerSync uses the 10c base rate until coverage is established. Future
days have separate allowances. Static tariffs sent to battery systems use the
10c base rate; the optimizer accounts for the limited premium separately.

Selecting this plan does not switch your retailer contract. Only select it after
confirming the plan and effective date with Flow Power.

<a id="octopus-energy"></a>

## Octopus Energy and BottlecapDave

PowerSync can use the electricity rates already loaded by the
HomeAssistant-OctopusEnergy integration. Rate events and entity changes are
consumed during a half-hour, including Intelligent Go adjustments. The normal
half-hour sync remains a fallback. Octopus/API detection latency still applies.

In PowerSync's Octopus settings, select the BottlecapDave import and export
meters when more than one is available. The selected source's actual tariff
overrides the fallback product preset, so published Cosy, Snug, Tracker,
fixed/variable and other interval schedules can be consumed without inventing
new hardcoded product codes. Select **Published tariff API (no Intelligent dispatches)**
for the direct API path, or **No export tariff (unknown)** to clear an export binding.
Export must belong to the selected account/site;
an unavailable export rate is unknown and earns no assumed revenue in planning.
It is not published as a live export price or used to trigger price curtailment.

The **Octopus source status** sensor exposes the binding, original/effective
prices, adjustment/cap flags, upstream timestamps, dispatch information,
Power Up/Down events and related account entities. Those related entities include
available gas, standing-charge, usage/cost, Home Mini/Home Pro, target and reward
monitoring. Their values retain their upstream timestamps and units; delayed
meter history is not live inverter telemetry or a reconciled bill. Large
collections are excluded from recorder attributes.

Intelligent prices follow BottlecapDave's planned/started policy. A planned
dispatch or dispatching binary sensor does not prove actual EV charging. Bump,
away, cancelled and provider-limited dispatches cannot independently establish
a household discount. Use the upstream integration's documented source policy
and diagnostics; PowerSync does not silently change those settings or control
the Octopus-managed EV. Qualifying household billing periods and actual charging
times are kept separate. Mixed five-minute planning cells use conservative prices.

The optimizer replans material changes within the same billing period, queues
the newest revision behind an in-flight solve and rejects superseded execution.
An economical allowed charge can start after that solve; a cheap rate does not
override SOC limits, grid-charge permissions, manual force modes, monitoring or
external control. Intelligent Flux retains Octopus battery ownership and is
monitoring only, including tariff uploads. Existing restoration/readback guards
still apply.

Current Power Down entities and their join service are supported alongside
legacy Saving Sessions. Existing legacy entity references are resolved to the
same account's renamed Power Down entity when available. Auto-join remains
opt-in; accepted joins stay pending until upstream joined state is reported.
Power Up/Free Electricity events are monitored separately. Baseline-dependent
credits do not turn the retail import price into zero, and missing reward rates
do not create an assumed optimizer payment.

Without BottlecapDave, the existing REST tariff path remains available. It
supplies published tariff rates, not personal Intelligent dispatch adjustments.
Update through HACS and restart Home Assistant. Inspect the source-status sensor
and selected meter after updating. Source/test/publication validation does not
establish installation or physical charging on an individual system.
