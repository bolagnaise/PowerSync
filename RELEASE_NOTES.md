<!-- release: v2.12.1323 -->

## Fixes

### EV power and Home Load frozen until a reload (Tesla BLE)

A Tesla BLE bridge that keeps re-reporting an unchanged watt value made that
value look permanently fresh, so a vehicle reporting `Stopped` could still be
credited with several kW of charging. `sensor.power_sync_ev_power` stayed
pinned with `is_charging: true`, and because Home Load subtracts observed EV
load, `sensor.power_sync_home_load` read 0.0 kW while the house was really
drawing 1.8 kW. Reloading the integration was the only way to clear it.

An explicitly stopped vehicle now retires the stale reading once it has held
that state past the attribution window, whichever entity updated last. The
same check now applies to solar-surplus accounting, which was adding the
phantom kilowatts to computed surplus and triggering repeated charge-start
attempts.

### Sigenergy: optimizer export could import from the grid

A force-discharge set the inverter's ESS discharge ceiling from the plan's
forecast battery figure. When live house load ran above the forecast, the
battery was capped below what the house was already drawing from it and the
shortfall came from the grid — so an export action spent the window importing
at full retail price instead. The ceiling now also covers the demand the site
is currently meeting from battery and grid. The grid-point export limit is
unchanged, so this adds battery headroom, not export.

### Sigenergy: solar curtailment stuck on "Pending"

With DC curtailment enabled, `sensor.power_sync_solar_curtailment` reported
`Pending` in every ordinary condition — including while exporting profitably —
and only returned to `Normal` if curtailment was switched off entirely. It now
reads `Normal` during ordinary economic export, matching every other brand.
`Pending` keeps its meaning: export is uneconomic, or a curtailment command has
been acknowledged but not yet physically confirmed.

### Powerwall pack capacity wrong for minutes after a reload

After reloading the integration, battery health could report a capacity total
that reflected fewer packs than the system actually has, then silently correct
itself a few minutes later. The corrected reading from the live Fleet/TEDAPI
poll was never written to storage, so each reload restored an older mobile
app WiFi-scan snapshot and published it as the current total until the next
poll ran. The live value is now saved when it changes, and the first poll runs
shortly after startup instead of a full interval later.
