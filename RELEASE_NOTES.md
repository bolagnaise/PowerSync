<!-- release: v2.12.560 -->

## What's Changed

**Fix: harmless "Unable to remove unknown job listener" warning on startup**
The startup timing change in 2.12.559 logged a one-off "Unable to remove unknown job listener" warning from the optimiser as Home Assistant finished starting. It was cosmetic — the optimiser still ran its first schedule normally — but the cleanup of the startup listener removed it twice. This release removes it exactly once, so the warning is gone.

Update available via HACS
