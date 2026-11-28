<!-- release: v2.12.1876 -->

## Zaptec no longer holds the home battery after Price Level stops

A Zaptec charger can stay in its "charging" state at 0 W after PowerSync stops it. When **Preserve Home Battery** was on, Price Level kept the home battery in no-discharge for as long as the charger stayed in that state, even though the car was not drawing any power and the price was above your EV limit.

PowerSync now releases the battery hold once the charger reports no power and no phase current after the stop. It still keeps the hold while the car is drawing power, or while there is no fresh charger reading.

Update through HACS and restart Home Assistant.
