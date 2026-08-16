<!-- release: v2.12.1121 -->

## What's Changed

**Powerwall alert sensors now distinguish normal status records from faults**
PowerSync now filters known informational Tesla local/V1R records such as successful firmware updates, grid connection status, grid-code writes, commissioning timestamps, and battery calibration from the actionable alert count and problem binary sensor. These records remain visible in dedicated informational and complete raw-detail attributes for diagnostics.

**Unknown and performance alerts remain fail-closed**
Unrecognized alerts and known actionable conditions such as `SiteMeterComms` continue to activate the problem sensors. Explicitly informational severities are filtered, while severity and raw alert evidence remain available through entity attributes.

**Vehicle-to-Home planning boundaries are now documented**
The repository now includes a provider-neutral V2H architecture covering signed-power observation, session-scoped energy accounting, planning-only import offset, capability proof, command ownership, and safe restoration. This is an architecture proposal only: this release adds no V2H controls, commands, or user-facing settings.

Update available via HACS
