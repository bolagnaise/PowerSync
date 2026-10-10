<!-- release: v2.12.1370 -->

## What's Changed

**Restore provider sensors after an asynchronous refresh**

GloBird account, billing, usage, and readiness sensors now register successfully after a portal refresh resumes from an asynchronous task. Provider pricing devices resolve the real PowerSync parent device ID, avoiding Home Assistant's deprecated device-link metadata while preserving the device hierarchy.

**Keep adjacent provider and Powerwall links compatible**

Flow Power, CovaU, and Powerwall child devices use the same resolved registry IDs, with a compatibility fallback for older Home Assistant versions. Stable entity IDs and existing provider data paths are unchanged.

Update available via HACS
