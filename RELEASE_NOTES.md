<!-- release: v2.12.867 -->

## What's Changed

**EV Smart Schedules now use the full forward forecast**
Amber history no longer consumes the requested forecast horizon. Cheapest, Prefer Solar, Solar Only, and Meet Deadline plans now stay between the current time and the configured departure, so past hours cannot appear as charging opportunities.

**Solar and price forecasts are matched by time**
Price and solar rows are now joined by their actual forecast hour instead of list position. Plans retain valid grid hours when the solar horizon is shorter, and repeated daylight-saving hours remain distinct through both planning and live charging-window activation.

Update available via HACS
