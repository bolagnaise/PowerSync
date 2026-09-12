<!-- release: v2.12.1278 -->

## What's Changed

**SolarEdge Self Consumption works again after update**
Fixed a SolarEdge coordinator routing error that could stop optimizer and manual Self Consumption requests before the native SolarEdge self-use command was sent. PowerSync now forwards the request through the existing control-health gate and confirmation path, so rejected or unconfirmed requests remain safely reported as failures.

Update available via HACS
