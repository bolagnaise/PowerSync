# Paired Powerwall charge and export targets

Paired Powerwalls with a reachable LAN gateway use timed local battery control for manual charge/discharge and optimiser requests. Charge kW targets battery input. Discharge kW targets grid export, adjusting battery output for current home load and solar. Fresh telemetry, reserves, charging permission, configured power limits and gateway site limits still apply.

## Gateway command limits

The gateway can report a timed-command limit below the combined Powerwall rating. From v2.12.1477, PowerSync learns charge and discharge ceilings independently from an exact matching gateway rejection. It requests normal operation, requires the gateway’s acknowledgement, and permits one retry with fresh safety checks inside the original time window. Later commands and the manual power controls use that ceiling; the configured battery rating is unchanged. An unconfirmed resume blocks further dispatch. Tariff fallback cannot bypass a learned ceiling or enforce a selected kW target.

A four-Powerwall 2 site on firmware 26.34.0 reported a 10 kW timed charging ceiling despite its configured 20 kW combined rating. The reporter demonstrated 10 kW charging and 10 kW grid export. This is evidence for that system, not a universal Powerwall 2 limit or a guarantee for other firmware. Check measured battery and grid power; acknowledgement alone does not prove output.

## Spread Import, Spread Export and EV charging

Home Assistant's Spread Import distributes battery charging across eligible same-price windows. Spread Export distributes planned export across eligible windows. They retain reserve, price, ownership and connection limits; Cloud does not expose these HA switches.

The optimiser already plans PowerSync-managed EV charging alongside the battery and treats supported native Tesla charge windows as fixed forecast demand without taking vehicle control. Spread Import now reserves the managed EV's planned power when redistributing battery energy, in addition to native Tesla demand already included in the load forecast. These sources are counted separately, once each.

Every local battery lease uses fresh measured home load, including current EV consumption, to limit battery charging to available import headroom. Forecasts help plan future demand; they do not guarantee an unseen vehicle schedule or unreported load. Keep gateway site connection limits configured: software targets do not replace enforced electrical limits.
