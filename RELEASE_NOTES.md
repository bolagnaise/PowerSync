<!-- release: v2.12.1776 -->

## Smart Schedule charging

Prefer Solar now uses free grid power when the home and battery leave too little solar to start the car and your charging rules allow grid use.

With Tesla **Both** mode, Smart Schedule can use a fresh battery level from the car's paired Bluetooth bridge when Fleet and Teslemetry have no usable reading.

## Tesla Charge on Solar settings

A failed read of Tesla's Charge on Solar setting no longer creates a charging hold when PowerSync was already free to manage the car. Existing holds stay in place while Tesla's ownership is uncertain.

Update through HACS and restart Home Assistant.

## Tesla BLE Stop recovery

The PedroKTFC companion now builds with ESPHome 2026.11. Update both PowerSync references in the existing bridge YAML to `v2.12.1776`, then rebuild and install the firmware using the [Stop recovery guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1776/docs/ev-command-recovery.md#tesla-ble-stop-recovery).
