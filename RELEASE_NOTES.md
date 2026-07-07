<!-- release: v2.12.783 -->

## What's Changed

**Priority export windows no longer idle**
PowerSync now prevents optimizer Idle holds inside explicit priority export windows such as Flow Power Happy Hour. If the LP tries to preserve SOC during one of those profitable export windows, the emitted schedule will export available energy above the export floor instead; if there is not enough export headroom, it falls back to self-consumption rather than an idle hold. This keeps Disable Idle Mode as a general preference while making high-value export windows behave like export windows by default.

Update available via HACS
