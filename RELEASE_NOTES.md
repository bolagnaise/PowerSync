<!-- release: v2.12.899 -->

## What's Changed

**Mobile Day energy summaries no longer mix dates after a restart**
PowerSync now detects the recorder reset pattern where some daily totals still carry yesterday's terminal values while other totals already reflect today. The Day view uses the coherent live daily snapshot instead of combining those two dates.

**Valid recorder history remains protected**
Small statistics differences continue to use the normal hourly reconciliation path, and a fresh or unrestored energy accumulator that is behind recorder history no longer causes valid current-day totals to be discarded.

Update available via HACS
