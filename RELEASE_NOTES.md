<!-- release: v2.12.1840 -->

## Tesla charging no longer blocked after a brief battery-level dropout

If a Tesla's battery level briefly stopped reporting, for example while the car was asleep, Solar Surplus could send a Stop to a car it had already stopped. A sleeping car can't confirm that Stop, so PowerSync then held every charging command for that car until it could check the result. The next Smart Schedule window, including free-power windows, could then fail with "Tesla command route is blocked". PowerSync now keeps the car stopped without sending another Stop when it stopped the car itself and the car reports it isn't charging. The same applies when Smart Schedule or Boost takes over from Solar Surplus. If there is any sign the car might be charging, PowerSync still sends the Stop as before.

Update through HACS and restart Home Assistant.
