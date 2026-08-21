<!-- release: v2.12.1177 -->

## What's Changed

**Sungrow Month Average now honours verified daily coverage gaps**
When Sungrow's daily or lifetime energy registers show imported or exported
energy that was not captured by PowerSync's sampled price ledger, the Month
Average now stays **Unknown** rather than presenting a potentially incomplete
figure. The known coverage gap persists through a Home Assistant restart and
the daily rollover; no cost is invented and no inverter-control behaviour is
changed.

Update available via HACS
