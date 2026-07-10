<!-- release: v2.12.819 -->

## Fixes

- Fixed Tesla/GloBird tariff summary and tariff schedule attributes showing high import rates such as `$10/kWh` as `10c/kWh` instead of `1000c/kWh`.
- Fixed Flow Power and other priority export windows being downgraded to self-consumption when forecast home load consumed the planned discharge. Priority export slots now still request export/discharge when there is SOC headroom above reserve.
