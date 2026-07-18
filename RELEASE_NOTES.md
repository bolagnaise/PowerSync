<!-- release: v2.12.888 -->

## What's Changed

**Stabilise load forecasts when Recorder raw history is shorter than expected**

PowerSync now supplements normalized raw load states with Home Assistant's
hourly long-term statistics. Installations with 30 days of Recorder statistics
but a shorter raw-state window can therefore use the older baseline instead of
forecasting from only the newest few days. Raw readings are integrated into
energy-weighted half-hour buckets, so sensors that update frequently no longer
count as extra historical evidence.

**Prevent one recent anomaly from multiplying the entire forecast**

The former whole-horizon recent-load multiplier has been replaced by
confidence-weighted adjustments for matching local clock-time buckets. Sparse
evidence is pulled toward neutral, temperature effects are applied only once,
and an unusual daytime load can no longer inflate unrelated evening demand up
to the old 2.5x cap. Away Mode retains its deliberate whole-home scaling while
the house is unoccupied.

Forecast sensors now expose compact history-source and recent-adjustment
diagnostics so Recorder coverage and scaling decisions can be verified from
Home Assistant.

Update available via HACS
