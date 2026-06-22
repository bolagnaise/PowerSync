<!-- release: v2.12.689 -->

## What's Changed

**Sungrow forced discharge preserves the inverter max discharge limit**
PowerSync no longer lowers the Sungrow SH max battery discharge register when the optimizer issues a forced-discharge target. The optimizer now sends the target through Sungrow's forced-power command while leaving the inverter's configured maximum discharge capacity available for normal home-load support.

Update available via HACS
