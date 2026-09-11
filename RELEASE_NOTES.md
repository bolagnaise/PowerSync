<!-- release: v2.12.1275 -->

## What's Changed

**SolarEdge self-consumption now selects the native self-use command**
Manual and optimizer self-consumption requests now explicitly select and confirm SolarEdge Modbus Multi's supported self-use command instead of treating a no-op restore as a successful override. Requests that cannot select or confirm that command continue to fail safely without activating the override.

Update available via HACS
