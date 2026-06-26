<!-- release: v2.12.720 -->

## What's Changed

**Tesla force charge now respects actual solar surplus**
PowerSync no longer backs out of a planned Tesla force-charge window just because live solar is present. The Tesla AC-coupled solar protection now checks whether that solar is actually surplus after site load, battery flow, and grid import are considered, so large intentional loads such as scheduled EV charging can still use free-import battery charging instead of draining the Powerwall.

Update available via HACS
