# SAJ H2 / HS2

PowerSync reads telemetry and controls SAJ H2 / HS2 systems through the
[SAJ H2 Inverter Modbus integration](https://github.com/stanus74/home-assistant-saj-h2-modbus).
Configure the inverter's rated AC power accurately in PowerSync: it is used to
convert the inverter's power-setting percentages to watts.

## Power-setting sensors in SAJ Modbus 3.1.3

SAJ Modbus 3.1.3 changes eight power-setting sensors from percentages to unitless
raw register values. For example, `50 %` becomes `500`, and `1100` means 110% of
rated power. Live solar, grid and battery power readings are separate sensors.

PowerSync detects the format from each power-limit sensor's unit and supports
both the older percentage sensors and the newer raw values. On a 10 kW inverter,
an older limit of `50 %` and a newer raw limit of `500` both report 5 kW in
PowerSync. Raw `100` means 1 kW on that inverter, and a zero limit reports zero.
Current-limit sensors are not interpreted as power percentages; if no usable
power-setting sensor is available, PowerSync retains its configured rated-power
fallback.

Update PowerSync through HACS and restart Home Assistant for this compatibility
fix. No manual multiplication is needed for PowerSync's own limits. Any personal
automations, templates or dashboards that compare the eight upstream sensors
directly still need their thresholds multiplied by ten, as described in the
[upstream changelog](https://github.com/stanus74/home-assistant-saj-h2-modbus/blob/main/CHANGELOG.md).
The upstream integration may also show a one-time statistics repair notice under
Developer Tools → Statistics.

TOU charge/discharge slot power inputs continue to use ordinary percentages.
Do not multiply those values by ten.
