<!-- release: v2.12.1214 -->

## What's Changed

**Retry GoodWe export limiting when physical export remains visible**
For direct GoodWe control profiles, PowerSync now makes one throttled retry
when a successful zero-export register write is followed by fresh inverter
telemetry still showing more than 250 W exported during an uneconomic export
period. The existing 15-minute enforcement retry remains in place, while the
new 60-second guard prevents frequent price updates from repeatedly writing
the inverter.

The solar-curtailment status remains Pending until current telemetry confirms
the physical export is below the threshold. Register acknowledgement alone is
still not treated as proof that the inverter stopped exporting.

Update available via HACS
