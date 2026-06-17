<!-- release: v2.12.663 -->

## What's Changed

**Multi-Tesla scheduled charging plug detection**
Scheduled charging now keeps scanning Tesla Fleet and Teslemetry vehicles when the legacy no-VIN path sees the first vehicle unplugged. This fixes multi-car homes where one Tesla is away or unplugged while another Tesla is at home and plugged in, preventing the mobile app and scheduler from incorrectly reporting "Vehicle not plugged in" for the whole setup.

**External scheduled-session guard clarity**
The scheduled charging external-session guard now stops carrying an away-vehicle reason forward once it has seen another Tesla at home. When no home vehicle is actively charging, the guard reports that there is no active external scheduled session instead of saying the away car is blocking the loadpoint.

Update available via HACS
