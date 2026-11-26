<!-- release: v2.12.1850 -->

## Tesla Bluetooth charging no longer stays paused after an unconfirmed Stop

If a Bluetooth Stop went out but the car never sent a fresh reading to confirm it, PowerSync paused charging control for that car until it could confirm the car had stopped. If a solar surplus or scheduled charging session was running at the time, the pause could last indefinitely. The session kept its hold on the car while it waited, and automatic recovery would not check the car while a session held it. The app showed **Charging control paused**, and planned charging (for example a Meet Deadline schedule) could not start.

Automatic recovery now checks the car even while a charging session holds it, as long as no charging command is in progress. Once Tesla's cloud reports the car stopped, the pause clears and the session carries on. This applies to Tesla vehicles controlled through an ESPHome Bluetooth bridge. Update through HACS and restart Home Assistant.
