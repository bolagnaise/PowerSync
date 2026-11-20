<!-- release: v2.12.1763 -->

## Tesla Solar Surplus on Wall Connectors

Solar Surplus now checks whether the Wall Connector’s contactor is open before treating a small standby reading as charging. This lets PowerSync start an idle Tesla when enough solar is available.

The EV screen can also show live charging current from the Wall Connector matched to that car.

## Manual charging with Fleet and Teslemetry

If both integrations report a location for the same car, **Charge now** uses the latest report. If equally recent reports disagree, an away report blocks charging.

Update through HACS and restart Home Assistant.
