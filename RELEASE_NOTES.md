<!-- release: v2.12.1873 -->

## Install a Tesla Bluetooth bridge from your browser

The new [Tesla BLE installer](https://bolagnaise.github.io/PowerSync/ble/) flashes a ready-made Bluetooth bridge with the PowerSync companion and a 48 A maximum onto an ESP32, ESP32-S3, ESP32-C5 or ESP32-C6 over USB. You don't need ESPHome, YAML or passwords. Use Chrome or Edge on a computer.

1. Plug the board in, select **Install Tesla BLE bridge** and enter your Wi-Fi when asked.
2. Add the **Tesla BLE** device Home Assistant discovers.
3. PowerSync fills in the bridge's **Tesla VIN** from your Tesla integration within about 15 minutes. It does this when you have one car and one bridge, or a VIN-to-bridge mapping. Otherwise, type the VIN in yourself.
4. In the car with your key card, press **Pair BLE key** and accept the key on the car's screen.

It also replaces a bridge whose YAML or OTA password you no longer have. Installing erases the board, so you pair the car again. See [Install a new bridge from your browser](https://github.com/bolagnaise/PowerSync/blob/main/docs/tesla-ble.md#install-a-new-bridge-from-your-browser).

Update through HACS and restart Home Assistant.
