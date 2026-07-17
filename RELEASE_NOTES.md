<!-- release: v2.12.882 -->

## What's Changed

**Keep Solcast forecasts stable across midnight**

PowerSync now uses Solcast's full cached forecast before falling back to the daily Today and Tomorrow sensor attributes. This keeps the rolling 48-hour optimizer horizon complete late in the evening instead of temporarily treating the missing third calendar day as zero solar.

This fixes isolated five-minute charge actions at midnight that could disappear on the next optimization cycle when Solcast's daily sensors rolled forward.

Update available via HACS
