## What's Changed

**Tesla Powerwall — gateway IP also editable from the integration's Configure dialog**
The previous release added the gateway local IP field to the initial setup wizard, which only helped new installs. Existing users had no way to set the IP from inside Home Assistant — the only path was the mobile app's Battery Setup → Gateway Connection screen. The Tesla Connection options page (Settings → Devices & Services → PowerSync → Configure → Tesla Connection) now exposes the same field so the IP can be added or cleared without touching the mobile app. Clearing the field reverts the install to cloud-only mode and the new diagnostic binary sensor flips back to `off`.

Update available via HACS
