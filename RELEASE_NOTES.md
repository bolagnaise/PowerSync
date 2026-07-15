<!-- release: v2.12.854 -->

## What's Changed

**Sigenergy cloud token refreshes no longer restart PowerSync**

Fixed refreshed Sigenergy cloud credentials being saved as a normal config-entry update, which caused the entire PowerSync integration to reload after token renewal. Token updates are now persisted without a reload, while setup-time, unchanged, and failed writes cannot suppress a later genuine settings update.

Update available via HACS
