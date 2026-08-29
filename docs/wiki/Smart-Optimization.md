# Smart Optimization

Smart Optimization is PowerSync's built-in LP battery scheduler. It plans charge,
discharge, export, and self-consumption actions from electricity prices, solar
forecast, household load forecast, battery limits, and configured reserve floors.

Solar forecasting via Solcast or Open-Meteo Solar Forecast should be configured
for accurate schedules.

## AI Plan Explanation

AI Plan Explanation is optional. It turns an existing deterministic Smart
Optimization plan into a homeowner-oriented explanation; it cannot control,
execute, modify, or recommend changes to the optimizer plan, settings, battery,
EV, tariff, or hardware.

### Gemini availability and diagnostics

PowerSync does not itself require a paid Gemini tier. Gemini
`gemini-3.5-flash-lite` can work with a Gemini API free-tier key when Google
allows that key to use the model and quota is available. Google controls project
and account eligibility, regional availability, model access, quota, and rate
limits, so a free-tier key can still be rejected or rate-limited. PowerSync
cannot promise free-tier access for every Google account, project, or region.

To distinguish a provider eligibility or quota problem from a PowerSync setting,
test the same key directly. This request mirrors the integration's current
Gemini endpoint, API revision, and model. Run it in an empty local directory;
it writes the response headers and body there. Enter the key only at the prompt
so it is not put in shell history, and never post, paste, or share the key.

```sh
read -rsp "Gemini API key: " GEMINI_API_KEY; export GEMINI_API_KEY; printf '\n'
curl --silent --show-error \
  --dump-header gemini-response.headers \
  --output gemini-response.json \
  --write-out 'HTTP status: %{http_code}\n' \
  --request POST 'https://generativelanguage.googleapis.com/v1beta/interactions' \
  --header 'Content-Type: application/json' \
  --header "x-goog-api-key: $GEMINI_API_KEY" \
  --header 'Api-Revision: 2026-05-20' \
  --data '{
    "model": "gemini-3.5-flash-lite",
    "input": "Return only valid JSON: {\"ok\": true}.",
    "store": false,
    "generation_config": {"max_output_tokens": 32},
    "response_format": {
      "type": "text",
      "mime_type": "application/json",
      "schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": false
      }
    }
  }'
unset GEMINI_API_KEY
```

Keep the displayed HTTP status and the exact raw provider response locally as
the provider-error evidence. Before sharing a diagnostic, make a redacted copy
of the headers and response that removes the API key and any account, project,
or request identifiers; do not alter the local original.

### Grok billing

xAI Grok API calls are separately metered. They are not included with an X or
Grok consumer subscription, so use an xAI API account with applicable billing
and usage limits.

## Core controls

### Enable Smart Optimization

Turns the PowerSync LP scheduler on or off. When disabled, PowerSync keeps the
saved Smart Optimization settings but does not own battery dispatch.

### Minimum discharge level

The software boundary for intentional battery-to-grid export. Natural
self-consumption may continue below this level to the separate hardware backup
reserve. Merely allowing export in a slot does not turn this value into a global
SOC hold or recharge target.

### Auto-apply optimizer reserve

When enabled, the selected minimum discharge level becomes the buffer that the
forecast should retain until the next charging opportunity. For each planned
export window, PowerSync adds the forecast net household load between the end of
the full eligible export window and the next grid or solar charge. The resulting
Calculated Reserve stops intentional export early enough for forecast
self-consumption to finish at the selected buffer.

Auto-Apply never lowers the Calculated Reserve below the selected minimum. It
does not change the hardware backup reserve or force the battery to recharge;
actual unforecast load can still consume the software buffer.

Because Calculated Reserve is one live software floor, only export episodes
that begin on the current local calendar day can raise it. A future-day export
episode is recalculated when that local day begins, so tomorrow's forecast load
cannot block an eligible export today.

### Hardware backup reserve

The battery's own backup reserve. PowerSync restores this value after temporary
hold or force-control modes.

### Timed manual controls in the Action Plan

When an external PowerSync control is active — Force Charge, Force Discharge,
Hold SoC, or timed Self-Consumption — Smart Optimization immediately rebuilds
the forward plan. The control is fixed for the slots covered by its timer, its
effect on forecast battery SOC is included, and the remaining horizon is then
optimized from that projected state. Canceling, replacing, extending, or
expiring the control triggers another plan rebuild.

The Action Plan/API labels these slots with `control_source: manual`, the
specific `control_action`, and `action_reason: manual_control_projection`.
These fields describe planned projection, not a new hardware acknowledgement;
the existing force-control state and device telemetry remain the evidence of
what the battery actually accepted. Monitoring-only connection methods remain
write-free.

### Allow grid charging

When enabled, Smart Optimization may plan forced battery charging from grid
import when prices make it worthwhile. When disabled, Charge By Time cannot force
grid charging, but solar surplus can still charge the battery naturally.

### GloBird ZeroHero and ZeroCharge

GloBird ZeroHero terms vary by account and start date. Select the preset that
matches your written GloBird plan, such as `ZeroHero Jul 2026`, `previous
3-hour`, `legacy 2-hour`, or `custom / account-specific`. PowerSync does not
auto-migrate existing saved ZeroHero settings.

Base import and feed-in rates still come from the Tesla tariff or PowerSync
custom tariff. ZeroHero Super Export is modeled separately as a capped export
top-up, and ZeroCharge is modeled separately as a capped free-import window.
For Jul 2026 terms this means a 12:00-15:00 free-import window with a 50 kWh
daily cap, plus the 18:00-21:00 Super Export/no-import window.

### CovaU SolarMax

CovaU is configured as an electricity provider. PowerSync supports the current,
fixture-backed SolarMax products for Ausgrid, Endeavour Energy, Essential
Energy, Energex and SA Power Networks. Postcode filters the candidates; setup
still requires confirmation of the exact distributor and AER plan ID.

The selected public AER/CDR plan response and normalized tariff are cached as an
immutable snapshot. A withdrawn plan is never silently replaced with a
successor. If a public plan is unavailable or account-specific, setup provides a
validated manual stepped-tariff fallback.

SolarMax allowances are settled from measured PCC energy, not from the
optimizer schedule. Select cumulative `total_increasing` import and export
energy sensors where possible. Power-integrated estimates are accepted only
while telemetry remains continuous. A telemetry gap or a first setup without a
valid tariff-day baseline marks quota confidence unknown and disables quota
bonus optimization until the next reset.

CovaU tariff windows follow the Home Assistant timezone, including daylight
saving. For example, an advertised `11:00-14:00` South Australian window
remains `11:00-14:00` in Adelaide rather than being shifted by the public CDR
record's `AEST` token. Current price sensors show the effective marginal price,
and the CovaU sensors/API expose cap, settled, remaining and planned quota
values explicitly in kWh.

## Network export limits / Flexible Exports

Flexible Exports is a separate network constraint, not an electricity provider.
PowerSync reads a limit exposed by already-certified site equipment through Home
Assistant. It does not implement IEEE 2030.5, certificates, NEPKI registration,
SAPN onboarding or DERControl writes, and it must not be described as a
CSIP-AUS-certified client.

The default mode is **Off**, which preserves existing behavior. **Monitoring**
shows the envelope and suppresses intentional PowerSync export. This release is
monitoring-only while the required seven-day SAPN site soak and staged
fallback/recovery replay are completed. The tested **Active** implementation is
held behind a runtime release gate and cannot be selected or armed.

When Active is enabled in a later release, it will remain an explicit opt-in and
will arm only after a fresh post-subscription update, trusted non-template
entity provenance, a site-approved fallback, fresh PCC telemetry, whole-site
DER coverage attestation, and a safe site phase/scope combination.

Active enforcement uses the lower of the existing static export cap and the
valid live envelope. Invalid or missing live data uses the approved fallback; a
missing fallback fails closed to 0 W. The runtime guard also reserves at least
250 W or 5% of the effective limit and accounts for unmanaged PCC export before
allowing a battery export command. A source fault, stale PCC value, overshoot or
failed stop command disables intentional export and remains visible in Home
Assistant and the mobile app.

There is no writable network-limit, override or bypass endpoint. The certified
controller remains authoritative and must continue enforcing the connection
agreement when Home Assistant or PowerSync is offline.

## Advanced optimizer controls

Advanced controls change the LP solver's decision boundaries. Leave them at their
defaults unless you want a hard rule that overrides the optimiser's whole-plan
economics.

### Maximum grid charge price

Sets a hard import-price ceiling for forced grid battery charging. For example,
`30c/kWh` means Smart Optimization will not plan forced grid charging in slots
above `30c/kWh`, even if Profit Max or a later high-price period would otherwise
make that charge look worthwhile.

Set this to `0` to disable the price ceiling. The limit only applies to forced
grid charging; solar surplus can still charge the battery.

### Grid charge SOC cap

Limits forced grid charging once the forecast battery SOC reaches the configured
cap. For example, `80%` lets the optimiser top up from grid when needed but stops
grid top-up above `80%`.

This is not the same as the Charge By Time target SOC or the hardware backup
reserve. The cap limits grid top-up; it does not stop solar from filling the
battery above the cap, and it does not change the battery's outage reserve.

### Import/export and spread controls

The maximum grid import/export, spread import/export, No Idle, and auto-applied
reserve controls are also advanced settings because they change solver limits or
the final physical trajectory. They are grouped with the grid-charge price and SOC
cap in the mobile app.

## Profit Max

Profit Max makes the optimizer more willing to export stored energy for profit
instead of holding battery charge for later use. It does this by lowering the
value assigned to ending the forecast horizon with a high battery SOC.

Profit Max does not, by itself, force the battery to be full by a deadline. Use
Charge By Time for that behavior.

Profit Max uses the same reserve model as normal Smart Optimization: intentional
export stops at the active optimizer reserve, while later household
self-consumption may continue to the hardware reserve. Profit Max by itself does
not add a hidden home-load bridge or require an overnight top-up. When Auto-Apply
Optimizer Reserve is enabled, its explicit forecast bridge raises only the
intentional-export floor. Grid charging is scheduled only when the modeled tariff
value, efficiency, limits, and future load/export value make it worthwhile.
Provider priority is permission, not a synthetic subsidy: export below the
modeled acquisition cost is allowed only when an actual, reachable quantity of
cheaper future recharge is paired with it.

On systems with a reversible, readable charge-only control, Profit Max can also
defer solar charging during a high feed-in-price interval and export that solar
directly, then replenish the battery from a cheaper, reachable solar or
grid-charge interval later in the 48-hour plan. This is an intrinsic Profit Max
decision, not a separate switch or target SOC.

The replenishment has to survive the solve, not just the estimate. A hold is
selected before the plan is built, from the charge capacity each later interval
could physically absorb, and on a charge-capacity-constrained day the rest of
the plan may already need that capacity. PowerSync therefore re-checks every
hold against the plan it actually produced: if the plan grid-charges later in
the horizon at more than the feed-in price the hold was selling at, that hold is
released and the plan is re-solved without it. Released holds are reported as
`profit_max_solar_export.capability.post_solve_revision` with reason
`grid_replenishment_costlier_than_export`, and holds whose feed-in price still
beats the plan's own grid-charge cost are kept.

The supported control paths are Sigenergy Modbus, Sungrow SH Modbus (including
dual-inverter systems), FoxESS Modbus/entity/cloud variants with authoritative
charge-limit readback, SolaX Modbus entities, Fronius Reserva/GEN24 Block
Charging, and Neovolt No Battery Charge. Availability is checked for the actual
configured variant on every plan and again before control. A supported family
therefore remains on normal control when its required entity, device, current
normal value, or verification readback is unavailable.

A solar-export hold also requires a known finite site export cap. PowerSync
auto-detects one only where the battery connection reports its own limit —
Sigenergy and AlphaESS from their configured export limit, and Fronius
Reserva/GEN24 from the inverter's Export Limit Control soft limit when the
`fronius_modbus` web API is configured and that soft limit is enabled. On every
other path, and on Fronius sites without the soft limit, set **Maximum grid
export** under Smart Optimization → Grid & site constraints. While it is blank,
`profit_max_solar_export.capability.reason` reads `export_limit_not_configured`,
a repair is raised naming the setting, and every candidate slot falls back to
self-consumption. A deliberate 0 reports `zero_export_site` instead and raises no
repair.

PowerSync persists the exact control path, every target, and each target's normal
value before changing hardware. It verifies all targets before reporting Solar
Export. Any preparation, write, verification, or restoration failure immediately
issues normal restoration and executes ordinary self-consumption; incomplete
cleanup is retried after reload and prevents another hold. Tesla, GoodWe,
AlphaESS, ESY Sunhome, SAJ H2, SolarEdge, Anker Solix, and Custom systems remain
on normal control because their current PowerSync control surfaces do not prove
an independently reversible charge-only hold. Monitoring mode also stays
write-free after cleaning an older hold. Provider charge blocks remain
independent, and Charge By Time still wins: a solar-export hold is not planned
unless its replacement energy is reachable before the configured deadline.

The battery **Connection method** controls which integration owns telemetry and
dispatch. A monitoring-only method never writes, including manual PowerSync
services. Home Assistant-backed methods reuse the selected upstream config entry
instead of opening a parallel Modbus or local API client. When the method is
changed, PowerSync restores any active force or idle state through the old route
before it saves and reloads the new route; a failed restore leaves the previous
method selected.

For Flow Power users, Profit Max still unlocks the Flow Power Happy Hour export
window behavior: battery export is allowed during the configured Happy Hour
export period when the plan is profitable. Other providers rely on their export
price signals, export boost, saving session, or plan-specific bonus windows.

## Charge By Time

Charge By Time is an independent Smart Optimization control. When enabled,
PowerSync adds a pre-window SOC target to the LP plan:

- `Charge By Time target time`: local `HH:MM` or compact `HHMM` time.
- `Charge By Time target SOC`: battery SOC target to reach by that time.

The behavior is the same for all electricity providers. If the target time has
already passed in the current optimizer horizon, PowerSync uses the next matching
time in the horizon. The default target is `17:15` and the default target SOC is
`100%`.

Charge By Time only creates a fill-by deadline. It does not make export slots
eligible on its own. Export eligibility still comes from positive export prices,
Flow Power Profit Max Happy Hour behavior, export boost, saving sessions, or
provider-specific bonus windows.

## Spread controls

### Spread export across window

On supported batteries, Smart Optimization spreads planned battery export across
the eligible export window instead of using maximum discharge power immediately.

### Spread import across window

On supported batteries, Smart Optimization spreads planned grid charging across
same-price import windows instead of using maximum charge power immediately.

## No Idle mode

For every electricity provider, No Idle mode replaces optimizer idle hold actions
with self-consumption. No Idle takes precedence over Charge By Time, so PowerSync
does not retain an IDLE hold solely to keep the target SOC reachable. As a result,
the battery may miss the configured Charge By Time target when serving forecast
home load leaves too little charging time or headroom. The 24-hour Action Plan and
battery-power graph show those periods as self-consumption and battery-to-home
power.

## EV charging in the plan

When Smart Schedule EV charging is configured, the optimizer plans the car and
the home battery together against one grid import limit.

The home-load forecast deliberately excludes EV charging, so before this the
optimizer sized battery charge windows against the whole import limit while the
car quietly consumed part of it. The battery was then throttled at runtime to
keep the site under its meter limit and missed its charge target.

Each vehicle with outstanding energy and a charging deadline contributes its
*physical* charging envelope — from now until its deadline, at the charger's
rate. The solver chooses the timing within that envelope, so the car moves to
the cheapest slots that fit alongside the battery rather than simply charging
as early as possible. Multiple vehicles combine into one site demand; per-car
allocation stays with the EV controller, which already shares a site budget
between loadpoints.

Notes:

- **Delivery is a soft target.** If the car cannot physically finish in its
  window, the plan delivers as much as it can and logs the shortfall. It never
  makes the whole solve infeasible.
- **The plan is a ceiling, not a setpoint.** The EV controller may still charge
  below the planned figure for live site conditions, and start/stop stays with
  the EV planner — a stale or missing plan can never strand a plugged-in car at
  zero amps.
- **The greedy fallback still accounts for the car.** When the LP solver is
  unavailable, the heuristic cannot co-optimize, so it treats the car as known
  load charging as soon as its window allows. The battery plan is still correct;
  only the cost-shifting is lost.
- `ev_charge_w` appears on schedule slots that carry planned EV power.

## App and API fields

Current settings use these keys:

- `profit_max_enabled`
- `charge_by_time_enabled`
- `charge_by_time_target_time`
- `charge_by_time_target_soc`

For compatibility, the settings API still accepts and returns the legacy aliases
`profit_max_target_time` and `profit_max_target_soc`. New clients should use the
`charge_by_time_*` names.

## Migration notes

Existing installations that had Profit Max enabled before the Charge By Time
split are migrated with Charge By Time enabled, preserving the previous fill-by
behavior. Existing target time and target SOC values are copied to the new
Charge By Time settings.
