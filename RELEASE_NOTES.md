<!-- release: v2.12.1801 -->

## Ending Smart Schedule while waiting for solar

When Smart Schedule ends a Tesla BLE session that is still waiting for solar, PowerSync now skips the extra Stop if fresh vehicle readings confirm the car is idle. This avoids an unnecessary confirmation timeout that could leave EV power and Home Load unavailable. If charging is active or the readings are uncertain, PowerSync still sends Stop and checks for confirmation.

Update through HACS and restart Home Assistant.
