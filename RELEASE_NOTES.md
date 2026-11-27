<!-- release: v2.12.1867 -->

## OCPP charging history records the right energy and cost

Sessions on chargers connected through the HACS OCPP integration were recorded with about one thousandth of the energy actually delivered, so the kWh and cost in EV charging history were far too low. The OCPP integration reports charger power in kW, and PowerSync was converting it to kW a second time.

PowerSync now reads the power sensor's unit, so OCPP sessions record the real kWh and cost. OCPP sessions also use the same electricity price as other EV charging sessions, so non-Amber dynamic tariffs and time-of-use rates are priced correctly. A session that is still drawing power while the charger shows Finishing now stays open until charging stops.

This applies to OCPP chargers managed through the HACS OCPP integration. Sessions recorded before this update keep their old values. Update through HACS and restart Home Assistant.
