<!-- release: v2.12.1184 -->

## What's Changed

**Restore GoodWe export before a scheduled discharge**
Some GoodWe inverters report their inactive export-limit register as `0 W` after PowerSync disables the limiter, even after accepting the release write. PowerSync now treats the confirmed disabled enable flag as the effective release state, so that harmless normalized value no longer blocks optimizer or manual force discharge.

Active zero-export curtailment remains fail-closed: an enabled limiter must still confirm its exact limit, and unreadable or still-enabled states continue to prevent discharge.

Update available via HACS
