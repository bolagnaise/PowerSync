<!-- release: v2.12.1524 -->
## What's Changed

**Powerwall settings: local confirmation with cloud fallback**

Grid Charging, operating mode and grid-export preferences can now use a paired gateway for local changes and readback, including with Tariff control selected. Unpaired or unavailable local access falls back to confirmed cloud settings. Tariff uploads and backup reserve changes continue through cloud.

**Powerwall restoration and recovery**

Force preparation saves the original settings before changing them, and unfinished restoration keeps its recovery state across retries and restart. Observed settings come from readback rather than the requested switch position.

If a local settings reply is lost, Home Assistant Repairs provides instructions for the **Resume Powerwall settings through cloud** action. After explicit acknowledgement, cloud settings and restoration attempts remain available across restart. The uncertain write and original restore evidence are retained, and local settings writes remain suspended. The earlier full configuration write may still apply later, so supervise recovery and verify the intended settings.

Update through HACS and restart Home Assistant. Settings confirmation does not prove a physical battery response; check Battery Power and Grid Power.
