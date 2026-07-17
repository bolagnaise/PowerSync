<!-- release: v2.12.876 -->

## What's Changed

**Stop PowerSync authentication expiring across Home Assistant installations**
PowerSync now gives each Home Assistant config entry its own stable cloud session slot. Signing in again on one Home Assistant instance no longer deletes the bearer token used by another instance or entry on the same PowerSync account. Existing sessions adopt their entry identity automatically after updating, while web and mobile sessions remain isolated as before.

If an entry is already showing the **Authentication expired** repair, update first and complete that repair once. Its deleted bearer cannot be recovered, but later reauthentications will no longer invalidate the other Home Assistant installations.

Update available via HACS
