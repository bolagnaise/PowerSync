<!-- release: v2.12.905 -->

## What's Changed

### Manual force controls now restore correctly in Monitoring Mode

Force Charge and Force Discharge switches now retain their manual source when they call the underlying service. Timed force sessions can also perform their safety restore when they expire, preventing Monitoring Mode from blocking a user-started control or leaving the battery in force mode after the selected duration.

### Solar Surplus now ignores unplugged vehicles

PowerSync now checks the selected vehicle's live plug state before claiming Solar Surplus ownership or creating a charging session. An unplugged vehicle no longer causes repeated session start/cleanup churn or misleading unplugged notifications. Active sessions still use the existing two-sample debounce so a brief telemetry interruption does not stop real charging.

Update available via HACS
