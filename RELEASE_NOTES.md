<!-- release: v2.12.1643 -->

## Avoid unnecessary grid charging with solar curtailment enabled

The optimiser now keeps available solar in its forecast when planning battery charging, exports and EV demand. Previously, a separate estimate of when the battery would fill could discard usable solar and cause paid grid charging, even when forecast solar could meet the battery target.

Grid charging can still be selected when prices, load, reserves or a charge deadline require it. Explicit solar production stops during negative import prices continue to apply.

Update through HACS and restart Home Assistant. No curtailment settings need to change.
