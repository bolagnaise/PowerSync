# Smart Optimization

Smart Optimization is PowerSync's built-in LP battery scheduler. It plans charge,
discharge, export, and self-consumption actions from electricity prices, solar
forecast, household load forecast, battery limits, and configured reserve floors.

Solar forecasting via Solcast or Open-Meteo Solar Forecast should be configured
for accurate schedules.

## Core controls

### Enable Smart Optimization

Turns the PowerSync LP scheduler on or off. When disabled, PowerSync keeps the
saved Smart Optimization settings but does not own battery dispatch.

### Minimum discharge level

The optimizer reserve floor. Smart Optimization will not intentionally discharge
below this level. This is separate from the battery hardware backup reserve used
for grid outages.

### Hardware backup reserve

The battery's own backup reserve. PowerSync restores this value after temporary
hold or force-control modes.

### Allow grid charging

When enabled, Smart Optimization may plan forced battery charging from grid
import when prices make it worthwhile. When disabled, Charge By Time cannot force
grid charging, but solar surplus can still charge the battery naturally.

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
post-processing behavior. They are grouped with the grid-charge price and SOC cap
in the mobile app.

## Profit Max

Profit Max makes the optimizer more willing to export stored energy for profit
instead of holding battery charge for later use. It does this by lowering the
value assigned to ending the forecast horizon with a high battery SOC.

Profit Max does not, by itself, force the battery to be full by a deadline. Use
Charge By Time for that behavior.

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

For supported TOU plans, No Idle mode replaces optimizer idle hold actions with
self-consumption. If Charge By Time is active and the battery is below the target
SOC before the target time, PowerSync preserves the hold behavior needed to meet
the deadline.

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
