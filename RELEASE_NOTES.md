<!-- release: v2.12.1863 -->

## Powerwall 0 W battery hold keeps running

A locally paired Powerwall holding the battery at 0 W could stop partway through when the gateway briefly reported a slightly negative Home Load. The battery then went back to its normal operation. A 0 W hold doesn't depend on home load, so it now continues through these readings. Fixed charge and discharge power still stop on a negative Home Load reading, as before.

Update through HACS and restart Home Assistant.
