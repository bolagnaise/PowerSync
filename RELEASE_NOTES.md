<!-- release: v2.12.1079 -->

## What's Changed

**Import-only Amber plans now sync to Tesla from Home Assistant**
When Amber provides a complete import-price forecast but omits the feed-in channel, PowerSync now builds the required Tesla sell schedule with conservative zero export prices. The Home Assistant TOU dashboard can populate and the tariff upload can proceed instead of failing with 48 missing sell periods.

Sparse or incomplete import forecasts still abort the sync and preserve the last good tariff, so an Amber API outage cannot overwrite a valid schedule. This brings Home Assistant into line with the import-only handling already deployed in PowerSync Cloud.

Update available via HACS
