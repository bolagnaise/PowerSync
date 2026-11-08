<!-- release: v2.12.1662 -->

## Better use of stored energy in Cost Neutral

When the backup planner is used, Cost Neutral now keeps higher-rate export slots before lower-rate ones when applying the daily earnings limit. This avoids spending extra battery energy at a low rate when the planned higher-rate window can cover the same costs.

This correction covers ordinary export tariffs; manual controls and capped export-reward allowances keep their existing accounting. Plans can still change as forecasts, battery charge and the remaining daily costs change.

Update through HACS and restart Home Assistant.
