<!-- release: v2.12.1325 -->

## What's Changed

**Gap-filled prices no longer inflate the value of your stored energy**
PowerSync decides whether exporting from the battery is worth it by comparing
the feed-in price against what the energy in the battery cost to acquire. Where
it cannot prove the origin of some of that energy — typically what carried over
from the previous day — it values it at the median of your real import prices.

Your provider does not always return a usable price for every interval. When one
is missing, the price series is gap-filled by repeating the previous interval's
price so the solver still has a complete array to plan against. v2.12.1324
stopped those copies counting as real forecast at the *end* of the series, but a
gap in the *middle* was still filled and still counted, because a later priced
interval carried the coverage boundary past it. A leading gap was back-filled
from a later price the same way.

Those copies are indistinguishable from real data once written, so they went
into the median that values unknown carry-over energy. A single expensive
interval sitting in front of a gap could be repeated across hours of the series
and pull the median up with it. Because the export decision is a threshold, an
acquisition cost lifted that way can sit just above the evening feed-in price
and remove a planned evening export window that would otherwise have run — with
no command sent to the inverter and no error logged, which is what makes it look
like a plan vanishing for no reason.

PowerSync now records which intervals the provider actually priced and values
stored energy from those alone. Copied values are excluded wherever they sit in
the series — leading, interior or trailing.

Nothing else changes. The solver still runs on the full gap-filled horizon, so
planning is unaffected, and the app's price chart still shows the contiguous
series over the real forecast window. Only the acquisition-cost reference is
filtered. If your provider prices every interval, which is the normal case, the
reference is exactly what it was before.
