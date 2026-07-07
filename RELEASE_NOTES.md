<!-- release: v2.12.780 -->

## What's Changed

**Keep GloBird ZeroCharge grid charging inside the free window**
PowerSync now clamps GloBird ZeroCharge grid charging to the configured free-import window and remaining daily cap. This prevents spread-import smoothing or same-price tariff periods from extending battery charging past the account's ZeroCharge end time, including custom/grandfathered windows such as 11am-2pm.

Update available via HACS
