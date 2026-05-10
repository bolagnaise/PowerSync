<!-- release: v2.12.368 -->

## What's Changed

**Reserve-floor optimizer behavior**
Smart Optimization no longer converts reserve-floor periods into an IDLE hold. When the projected battery SOC reaches the configured optimizer reserve, the schedule stays in self-consumption so the inverter can operate naturally while the configured reserve remains the floor. IDLE is now reserved for deliberate hold periods above the reserve where preserving charge for a later window is useful.

**Schedule chart power reporting**
The optimization API now reports total battery charge/discharge power plus separate battery-to-home and battery-to-grid series. Dashboard and mobile chart consumers can show self-consumption discharge instead of displaying an unexplained SOC drop or reserve plateau with no matching battery-power context.

Update available via HACS
