<!-- release: v2.12.1208 -->

## What's Changed

**Tesla BLE supports current ESPHome entity names**
PowerSync now recognises the current yoziru ESPHome Tesla BLE battery, charging-state, charger-power, and plugged-in entities while retaining support for the legacy names. BLE vehicles can therefore report their live status consistently in EV views and scheduling decisions after the bridge update; this does not enable charging or send any new hardware commands.

Update available via HACS
