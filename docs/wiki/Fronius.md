# Fronius GEN24 Storage

PowerSync supports Fronius GEN24 storage systems, including BYD Battery-Box and Fronius Reserva batteries, through the Fronius Modbus companion integration.

## Prerequisites

- Install `callifo/fronius_modbus` from HACS.
- Configure your GEN24 inverter in that integration before adding it to PowerSync.
- Provide the Fronius local customer password in the Fronius Modbus integration when available. This lets the companion integration expose the Web API battery controls used for reliable charge-from-grid behavior.

## PowerSync Setup

1. Add the PowerSync integration.
2. Select `Fronius GEN24 storage (BYD/Reserva)` as the battery system.
3. Select the Fronius Modbus integration entry if more than one is configured.
4. Enter usable battery capacity and maximum charge/discharge power for optimizer fallback values.

PowerSync reads Home Assistant entities exposed by `fronius_modbus`; it does not open a second direct Modbus or Web API connection to the inverter.

## Supported Controls

- Force charge from grid
- Force discharge/export to grid
- Hold SOC
- Restore normal automatic storage control
- Set backup reserve / minimum SOC
- Smart Optimization dispatch

## Site export limit

Profit Max solar export needs a known finite site export cap before it will hold battery charging to push solar to the grid.

PowerSync auto-detects it from the `fronius_modbus` **Export soft limit** sensor. That sensor only exists when the Fronius Web API is configured in the companion integration, and it only reports a value while Export Limit Control's soft limit is enabled on the inverter.

If you do not have it, set **Maximum grid export** under Configure → Smart Optimization → Grid & site constraints to your site/DNSP export cap in kW. Leaving it blank keeps solar export permanently off; `sensor.power_sync_optimization_status` reports `profit_max_solar_export.capability.reason` as `export_limit_not_configured` and a repair is raised.

If setup reports missing Fronius storage entities, confirm that the Fronius Modbus integration has created storage `sensor`, `select`, and `number` entities and that the battery system is online.
