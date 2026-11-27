<!-- release: v2.12.1864 -->

## Tesla Bluetooth charging no longer stays paused after an unconfirmed command

When a Bluetooth (ESPHome) Tesla command couldn't be confirmed, PowerSync paused all charging control for that car with "Charging control paused". The pause could last a long time even though the car was fine.

- **Unchanged charge current is no longer re-sent.** If the car already reports the charge current PowerSync wants, PowerSync no longer sends it again. ESPHome doesn't report a value that hasn't changed, so the repeated command could never be confirmed and paused the car.
- **A lost charge current command no longer pauses charging.** Whether it reached the car or not, the car keeps charging at a valid rate, and the next rate change replaces it. PowerSync now clears it within about a minute.
- **A lost Start or Stop clears sooner.** PowerSync now waits 3 minutes instead of 15, then resumes from the first fresh charging state Tesla's cloud reports, whatever that state is. It checks every 5 minutes until it gets one.

Update through HACS and restart Home Assistant.
