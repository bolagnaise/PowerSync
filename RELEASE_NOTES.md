<!-- release: v2.12.722 -->

## What's Changed

**Flow Power KWatch forecast slot fix**
PowerSync now requests the first upcoming half-hour KWatch predispatch slot at runtime, matching the setup validation path. This prevents Flow Power forecast data from starting at the second upcoming half-hour window when the KWatch API is used as the price source.

Update available via HACS
