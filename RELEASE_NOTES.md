<!-- release: v2.12.1616 -->

## GoodWe load-following solar curtailment

Some GoodWe hybrids, such as the ESA/EHA-G20, accept PowerSync's 0 W export limit but keep exporting once the battery is full. Direct GoodWe control now has a second curtailment method: **EMS load following (BuyPower 0 W)**. It uses GoodWe's documented EMS BuyPower mode to hold grid exchange at 0 W against the inverter's own meter. Solar keeps powering the house and charging the battery, surplus solar is reduced, and the battery covers any shortfall. When prices recover, PowerSync restores your original EMS settings.

To try it, open **GoodWe connection** in the PowerSync options and set **Solar curtailment method** to **EMS load following (BuyPower 0 W)**. The default stays on the export limit. The method needs a working GoodWe meter and the factory EMS fallback setting. PowerSync checks the fallback and won't start if it is different. Force charge and force discharge take over from the hold while they run. Update through HACS and restart Home Assistant.
