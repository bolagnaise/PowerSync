<!-- release: v2.12.831 -->

## What's Changed

**Start optimizer force actions on schedule boundaries**
PowerSync now applies the already-planned charge, discharge, and export action as soon as the optimizer crosses a schedule boundary instead of waiting for the next full LP solve to finish. This prevents export or force-discharge windows from starting a couple of minutes late when forecast/history work is slow, while keeping the existing duplicate-command guard for simultaneous scheduler refreshes.

Update available via HACS
