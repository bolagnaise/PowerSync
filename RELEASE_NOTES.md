<!-- release: v2.12.1854 -->

## Boost no longer stops a Tesla that Smart Schedule is already charging

Pressing **Boost** on a Tesla that Smart Schedule was charging from solar could stop the car instead of boosting it. PowerSync stopped the running charge before starting the Boost. On Bluetooth-controlled cars that Stop paused further charging commands, so the Boost start was refused and the car was left stopped.

Boost now takes over a charge that PowerSync is already running, as long as the car's own readings show it is charging. The car keeps charging and the rate rises to the Boost current, with no Stop sent. If the car is not confirmed to be charging, Boost starts it as before. Update through HACS and restart Home Assistant.
