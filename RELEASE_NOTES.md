<!-- release: v2.12.827 -->

## What's Changed

**Steadier curtailment across every battery brand at price boundaries**
The hysteresis added in v2.12.821 for AC-coupled curtailment now also covers the brand-specific curtailment paths (Sungrow, Sigenergy, SolarEdge, GoodWe, FoxESS, AlphaESS and the WebSocket-driven checks). When the export price hovers around the ~1c threshold, curtailment engages at the same point as before but no longer rapidly toggles on and off with every price tick — fewer inverter writes and cleaner behavior on volatile days.

**Powerwall local readings immune to clock adjustments**
The freshness check that decides whether to trust a just-read local Powerwall value could be skewed by system clock adjustments (NTP steps), occasionally treating fresh readings as stale or stale ones as fresh. Freshness is now judged on a monotonic clock, so time synchronization events can't affect which reserve value PowerSync trusts.

Update available via HACS
