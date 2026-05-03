## What's Changed

**EV quick-charge controls from the mobile app**
You can now start or stop a manual EV charge directly from the mobile app's
Controls screen with a duration slider (30 minutes to 6 hours) and a
charge-mode toggle (standard solar-only, or grid-allowed). PowerSync schedules
an automatic stop when the timer elapses, and re-arming a charge cancels any
pending stop so a fresh session isn't cut short by an old timer. If another
mode (price level, scheduled charging, smart schedule) already owns the
charger, the app now tells you which mode is in control instead of bouncing
off a vague backend error.

**Multi-vehicle Tesla telemetry now routes per-vehicle**
For households with two or more Tesla vehicles on the same account, PowerSync
was occasionally attributing Wall Connector power and "connected" state to
the wrong active session because those signals are reported globally. The
widget data view now matches by VIN, vehicle id, and display name (with
substring fallback for embedded VINs) and only falls back to global Wall
Connector / charge-cable signals when there's a single Tesla on the account.
The right car shows as charging, with the right SOC and ETA.

**HACS auto-update no longer misses fresh releases or re-installs staged ones**
Scheduled auto-updates now force a HACS metadata refresh against GitHub
before checking for an available update — previously, if the daily run fired
before HACS's own coordinator had picked up the release, the update was
skipped and you'd wait until the next day. The runner also recognizes HACS's
"Restart of Home Assistant required" state and schedules the Home Assistant
restart directly instead of trying to re-install an update that's already
staged. A version-comparison fallback covers the rarer case where the entity
reports no pending update but the latest version on GitHub is newer than
what's installed.

**Reduced Powerwall log noise**
TEDAPI v1r POST entries for local Powerwall communication moved from INFO to
DEBUG, cutting steady-state log volume on Tesla setups without losing
diagnostic capability when DEBUG logging is enabled.

Update available via HACS
