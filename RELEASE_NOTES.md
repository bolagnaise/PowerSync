## What's Changed

**Away Mode Switch Now Appears Correctly**
The `switch.power_sync_away_mode` entity was never being registered despite the LP optimizer being active. The switch platform runs before the optimizer is initialized, so the check for the optimizer always found nothing. The fix defers switch creation until the optimizer is ready, using the same pattern as the optimizer's forecast sensors. The switch will now appear under your PowerSync device after reloading the integration.

**Away Mode — What It Does**
When enabled, the LP load forecaster skips the last 7 days of electricity consumption history and instead uses the 28 days before that. This prevents the optimizer from planning around near-zero house loads recorded while you were away, so it forecasts correctly for when you return home. Solar forecasting is unaffected.

*Update available via HACS*
