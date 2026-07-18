<!-- release: v2.12.889 -->

## What's Changed

**Keep Spread Export active across the full eligible window in every optimizer mode**

Spread Export now chooses one reserve-feasible export rate for the complete
eligible window instead of flattening the planned energy first and then
clipping the final slots as battery SOC approaches the reserve. Auto-Apply
Reserve, Profit Max, and No Idle may reduce the common export rate, but they no
longer shorten the export window.

The calculation follows the battery SOC through home consumption and preserved
charge slots while respecting the configured reserve, discharge and grid-export
limits, and the export energy selected by the optimizer. If home load or an
inverter limit makes a positive whole-window export physically impossible,
PowerSync now avoids front-loading a partial forced export.

Update available via HACS
