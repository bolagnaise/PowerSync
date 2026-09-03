<!-- release: v2.12.1227 -->

## What's Changed

**Reliable Enphase AC-inverter restore lifecycle**

PowerSync now reuses its retained AC-inverter controller across automatic,
manual, and fast load-following control paths. Manual restore closes that exact
controller on both success and failure, while reload also closes any retained
controller session. This prevents stale Enphase Envoy HTTP sessions from being
left behind after curtail/restore cycles and serializes refresh writes with
manual control.

The release does not add proportional inverter limiting or change GoodWe EMS
operating modes; those remain topology- and vendor-specific capabilities.

Update available via HACS
