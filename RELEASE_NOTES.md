<!-- release: v2.12.774 -->

## What's Changed

**Stabilized GloBird ZeroHero export windows**
PowerSync now treats active ZeroHero bonus windows with remaining export cap as explicit priority export windows. This keeps Smart Optimization committed to the export window across repeated solves instead of cycling back to self-consumption during the same bonus period.

**Regression coverage**
Added tests to keep ZeroHero priority export active while the bonus cap remains, and to disable the export window once the cap is exhausted.

Update available via HACS
