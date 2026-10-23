<!-- release: v2.12.1465 -->

## What's Changed

**More accurate stored battery costs**
Measured solar and grid charging is now accounted for before each optimization run checks the battery level, preventing the same charge from also being treated as energy of unknown origin and unnecessarily blocking profitable exports. Energy already recorded as unknown is not relabelled; that portion reduces as the battery discharges.

Update available via HACS
