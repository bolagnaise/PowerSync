<!-- release: v2.12.1487 -->

## What's Changed

**Powerwall Max discharge with an unset gateway export limit**
Powerwall Max discharge and larger-site optimiser export no longer reject a gateway that explicitly reports no export limit. PowerSync applies and confirms its configured export cap before starting tariff control, then restores the original setting after discharge settles; missing or invalid limits still block the action.

Update available via HACS
