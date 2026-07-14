<!-- release: v2.12.847 -->
## What's Changed

**Smart Schedule now enforces its selected charging windows without overriding battery settings**

Fixed Tesla charging that could continue before a planned cheap or solar window when the vehicle auto-started or Home Assistant had lost PowerSync's in-memory charging state. Smart Schedule now verifies the specific vehicle's physical charging state, respects manual and other PowerSync owners, and suppresses duplicate stop commands while Tesla telemetry catches up.

Future EV demand now requests home-battery preservation only when that vehicle's effective Preserve Home Battery setting is enabled. Turning the setting off clears a previous hold on the next cycle while keeping the EV charging plan intact.

**Legacy generic and OCPP charger capacity overrides are visible again**

Existing per-charger battery-capacity overrides are now exposed through the dedicated fallback field, so clients can distinguish and clear them without confusing them with shared integration defaults.

Update available via HACS
