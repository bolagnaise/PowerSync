<!-- release: v2.12.509 -->

## What's Changed

**Fronius GEN24 BYD battery support**
PowerSync now supports Fronius GEN24 storage systems using BYD Battery-Box batteries through the Fronius Modbus companion integration. The existing Fronius storage bridge now recognizes the current `callifo/fronius_modbus` entity layout while preserving compatibility with existing Fronius Reserva installs.

**Full Fronius storage control**
Fronius GEN24 storage users can use PowerSync force charge, force discharge, hold SoC, restore normal, backup reserve, telemetry, and Smart Optimization dispatch through the Home Assistant entities exposed by the Fronius Modbus integration.

**Clearer setup and documentation**
The setup flow and documentation now label this path as Fronius GEN24 storage for BYD and Reserva systems, with improved missing-entity validation when the upstream integration has not exposed the required storage controls.

Update available via HACS
