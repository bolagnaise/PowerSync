<!-- release: v2.12.803 -->

## What's Changed

**Tesla force-charge now checks whether live solar can hit the planned SOC**
PowerSync now keeps the planned Tesla force-charge active when the battery is not projected to reach the optimiser's charge-block target SOC from live solar alone. This preserves the existing solar-yield behaviour when solar charging is enough, but prevents short cheap-price windows from being missed when weak or late solar would leave the battery below the planned export target.

Update available via HACS
