<!-- release: v2.12.1872 -->

## No more "solar to spare" at night

The EV page could say several kW of solar was spare while the home battery was powering the car at night. The figure came from a site reading that had stopped updating. It now uses the same live readings that control charging, and shows no spare solar when none are available. Charging itself was not affected.

## Repairs notice for Tesla Bluetooth bridges that cap charging

Many Tesla Bluetooth (BLE) bridges were built with a 15 or 16 A charging maximum, so every BLE charging mode stayed at about 3.5 kW even when the car and charger allow more. PowerSync now shows a notice under **Settings > System > Repairs** when:

- a PedroKTFC-family bridge runs firmware that doesn't report the car's charging limit, or
- a bridge's maximum is below the limit the car reports.

The notice links to [Update your Tesla BLE bridge](https://github.com/bolagnaise/PowerSync/blob/main/docs/tesla-ble.md#update-your-tesla-ble-bridge). yoziru-family bridges aren't asked to switch firmware.

Update through HACS and restart Home Assistant.
