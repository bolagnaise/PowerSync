<!-- release: v2.12.1497 -->

## What's Changed

**Keep Powerwall tariff charging running across optimiser refreshes**
Paired Powerwalls using tariff control now keep that control method when the optimiser refreshes an active charge. Previously, a refresh could switch to a fixed-power request and interrupt charging.

**Allow Restore to finish while surplus solar charges the battery**
Restore now recognises normal solar charging after forced grid charging stops. It still waits for two fresh, balanced readings and retains control ownership while excess grid charging remains.

Update through HACS and restart Home Assistant.
