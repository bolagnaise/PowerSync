<!-- release: v2.12.1130 -->

## What's Changed

**Tesla Charge By Time now plans against the connected Wall Connector**
PowerSync now correlates a connected Tesla Wall Connector with its exact vehicle and uses the VIN-scoped live charging capability instead of retaining a stale lower TeslaBLE entity limit from another charger. Overnight and deadline schedules can therefore use the Wall Connector's available rate while still respecting the vehicle's configured ceiling and the site's charging limit.

**Current commands recover cleanly from stale BLE entity ranges**
When Home Assistant rejects a higher TeslaBLE current because that entity still exposes an older range, PowerSync recognizes the full Home Assistant range-error response, applies the safe local fallback, and continues to the exact VIN-scoped Tesla provider when the connected Wall Connector association is proven. The applied-current state follows the command that actually succeeded instead of permanently lowering later planner decisions.

**Multiple Wall Connectors are isolated by physical serial number**
Fleet, Teslemetry, and local Wall Connector entities are joined by the connector serial, allowing two connected Wall Connectors and two vehicles to resolve independently. Duplicate observations for the same connector are accepted only when they agree; conflicting VINs, duplicate connector assignments, missing serials, and disconnected connectors retain the conservative fallback.

**UMC and charger safety boundaries remain intact**
PowerSync still sends Tesla charging-current commands to the vehicle rather than directly controlling a Wall Connector or UMC. UMC pilot limits, ambiguous charger associations, configured vehicle limits, site limits, and physical Wall Connector power sharing remain authoritative and are never overridden by this change.

Update available via HACS
