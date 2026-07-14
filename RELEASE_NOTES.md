<!-- release: v2.12.851 -->

## What's Changed

**The Energy dashboard stays put while cards refresh on iPhone and iPad**

Fixed an iOS WebKit scroll jump that could repeatedly pull the Energy dashboard upward while live cards refreshed, making lower cards difficult to read in Safari and the Home Assistant iOS app. The layout now preserves its stable rendered height across transient card rerenders without running a continuous animation loop, then safely resets the height lock for responsive column changes, card visibility changes, and component reconnection.

Update available via HACS
