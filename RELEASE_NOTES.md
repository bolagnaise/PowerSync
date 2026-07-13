<!-- release: v2.12.838 -->

## What's Changed

**Reliable Flow Power Happy Hour boundary execution**

Flow Power price forecasts now retain the exact interval timestamps they were built from, so a cached export action at the 17:30 Happy Hour boundary cannot misread the preceding zero-rate slot and temporarily fall back to self-consumption. Price values and timestamps now advance together across successful replans and provider changes, including optimizer failure paths.

Update available via HACS
