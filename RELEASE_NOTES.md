<!-- release: v2.12.1478 -->
## Spread Import follows total grid power

On paired local Powerwalls, Spread Import now adjusts battery charging as measured home and EV demand changes. Forecast EV charging no longer leaves import headroom unused when the car is not drawing power. Manual Charge input remains a battery-power target.

## Charging limits and remaining energy

The optimiser now includes the gateway's reported power ceilings and fresh import limit when replanning the remaining charging window. If the current grid target cannot be reached, PowerSync keeps the applicable limits, exposes the limitation and requests a fresh plan. The grid target, planned battery power and acknowledged battery command are reported separately.
