<!-- release: v2.12.890 -->

## What's Changed

**Preserve Auto-Apply reserve bridges on flat export tariffs**

Auto-Apply Optimizer Reserve now distinguishes a real planned export episode
from a tariff that merely permits battery export throughout the forecast. On
always-positive export rates, PowerSync anchors the recommendation to the
manual-baseline export episode and keeps the saved manual reserve plus enough
forecast energy for household consumption until the next scheduled grid or
solar charge. This prevents an intentional export from leaving the battery at
its hardware reserve for hours before the next off-peak period.

**Keep two-pass export planning seed-independent**

Spread Export and short-gap handling now use the reserve modeled by each
optimizer pass. The final applied schedule retains the recommendation and
explanation calculated from the manual baseline, so a previous Auto-Apply value
cannot shorten the reference plan, erase the bridge, or overwrite its metadata.

Update available via HACS
