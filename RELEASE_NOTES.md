<!-- release: v2.12.449 -->

## What's Changed

**Sungrow Modbus transaction stability**
PowerSync now shares one Modbus request lock across its Sungrow SG solar-inverter and SH hybrid-battery controllers when they target the same host, port, and slave ID. This prevents PowerSync's own Sungrow polling, export-limit, and battery-control paths from overlapping on the same WiNet/LAN Modbus endpoint, which could surface as pymodbus transaction ID mismatch warnings and skipped responses.

**Sungrow regression coverage**
Added coverage for mixed SG and SH controller access to confirm requests remain serialized across both Sungrow controller types, including the test harness fixes needed to run the Sungrow controller suites together.

Update available via HACS
