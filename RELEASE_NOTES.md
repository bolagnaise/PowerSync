<!-- release: v2.12.858 -->

## What's Changed

**Tesla multi-pack battery health no longer spikes when relay telemetry omits a pack**
PowerSync now reconciles an intermittent Tesla Fleet relay pack undercount with the site's physical battery count when the aggregate full-pack energy proves the missing pack. This keeps the Battery Health denominator correct for affected multi-Powerwall 3 sites while retaining the existing safeguards against duplicate BMS modules and stale registered pack counts.

**Flat-price charging stays stable near the optimizer reserve**
Rolling plans no longer switch between immediate charging and self-consumption solely because SOC crossed the optimizer reserve margin. Equal-price charging is delayed consistently unless a real pre-window SOC deadline requires earlier charging, while cheap-import and positive-feed-in windows still charge within the economic window without creating export loops.

**Long self-consumption projections converge without spurious warnings**
The optimizer now compares each contiguous natural self-consumption run using interval-weighted battery energy. Exact command modes and physical constraints remain required, while harmless sub-slot drift no longer exhausts all eight projection passes or fills the log with non-convergence warnings.

Update available via HACS
