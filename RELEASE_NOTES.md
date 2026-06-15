<!-- release: v2.12.652 -->

## What's Changed

**FoxESS mobile energy graphs use interval history again**
PowerSync now recognizes the FoxESS-specific daily battery charge and discharge sensors when building the mobile app calendar-history response. This lets the Android app receive recorder interval rows instead of a single live cumulative daily total, so FoxESS battery charge and discharge day graphs should no longer compress into one late-day spike.

Update available via HACS
