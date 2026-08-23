<!-- release: v2.12.1185 -->

## What's Changed

**Keep away Teslas out of automated charging during location outages**
When a VIN-matched Tesla location tracker temporarily became unavailable after a Home Assistant or integration reload, PowerSync could lose its in-memory location cache and treat the known tracker outage as permissive `unknown`. Smart Schedule could then create a charging session and send wake, current, or start requests before the provider restored the vehicle's away state.

PowerSync now keeps that vehicle outside every automated Tesla command path until the known tracker reports again, a retained location is available, or paired local BLE evidence proves the car is home. Installations with no location provider remain compatible, and future EV demand can still be forecast without authorizing a current charger command.

Update available via HACS
