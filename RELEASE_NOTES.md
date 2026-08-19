<!-- release: v2.12.1145 -->

## Fixed

### Per-Phase Load Management could not be turned on (affects everyone on v2.12.1125–v2.12.1144)

The Home Power Setup screen in the mobile app showed **Per-Phase Load Management**
greyed out, with *"Update the PowerSync Home Assistant integration to configure this
feature"* — even on the latest release, and even after restarting Home Assistant.

The app asks Home Assistant whether it supports per-phase load management by calling
`GET /api/power_sync/ev/home_power/settings`. Since the feature shipped in v2.12.1125
that endpoint returned **HTTP 500 on every request**: the handler builds a default
settings object before checking whether the automation store exists, and the helper it
called required an argument it was not given. The app correctly failed closed, so the
capability was never advertised and the toggle stayed disabled. Because the toggle
could not be switched on, the (working) save request was never sent — the feature was
unreachable from the UI for its entire life.

The endpoint now returns the settings and the capability flags as intended, on both the
stored-settings path and the fresh-install fallback. Per-Phase Load Management is
switchable, and the L1/L2/L3 current entity fields appear once it is enabled.

If you saw this, the error was in your Home Assistant log at ERROR level as
`Error getting home power settings: normalize_home_power_settings() missing 1 required
positional argument`.

Nothing else changed: saving settings, the runtime allocator, and every other EV
endpoint were unaffected, and no existing configuration is altered by this update.
