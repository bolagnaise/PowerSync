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

If setup reports missing Fronius storage entities, confirm that the Fronius Modbus integration has created storage `sensor`, `select`, and `number` entities and that the battery system is online.
