# Tesla Start confirmation

PowerSync checks fresh physical charging evidence after a Tesla Start. A newer
native reading replaces the initial command observation only when its VIN,
provider, config entry, device and producer identity all match. Stale, future,
pre-command and unchanged-timestamp readings cannot confirm a new Start.

In Fleet API or Both mode, an old Home Assistant cache can trigger a bounded
read-only request through the already selected Fleet connection for that exact
vehicle. BLE-only mode does not acquire this fallback. This read does not wake
the car or repeat the Start command, and ownership, provider binding and request
deadline checks still apply before and after the request.

Charging state and positive measured current or power remain necessary. Command
acceptance, a UI state and a later independent physical reading are separate
proof levels. A failed confirmation must not be described as proof that the
vehicle never started.

The corresponding user documentation is in the wiki's Tesla Vehicle Charging
section of EV Smart Charging. No provider, automatic EV mode or monitoring
setting changes are required for this correction.
