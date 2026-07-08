<!-- release: v2.12.794 -->

## What's Changed

**Self-consumption override now behaves like a timed manual mode**
Manual self-consumption can now be started for the same timed durations used by force charge/discharge. PowerSync persists the active override across restarts, restores normal operation when the timer expires, exposes countdown metadata through the battery mode sensor, and updates the dashboard action so the selected duration is clear before activation.

**Away-mode load forecasts now follow the active low-load regime**
When Away Mode is currently active, PowerSync now weights the load forecast toward the actual load seen since Away Mode was enabled instead of blending it with the previous occupied-house baseline. This prevents the optimizer from overestimating household demand while the home is away and makes charging/export plans better reflect the current low-load state.

Update available via HACS
