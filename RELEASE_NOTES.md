<!-- release: v2.12.781 -->

## Fixes

- Fixed GloBird ZeroHero Super Export planning when the base export tariff is 0c/kWh and the Super Export value is modelled as a capped bonus. Explicit priority export windows now use the effective export value, so a full battery can export above the bridge-to-next-charge floor instead of staying in self-consumption until the next free charge window.

## Validation

- Added a regression covering a 0c base export window with a capped Super Export bonus, high current SOC, and a later free-charge window.
- Verified the optimizer export/ZeroHero test set with `python3.12`.
