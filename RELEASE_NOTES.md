<!-- release: v2.12.1140 -->

## What's Changed

**Charge By Time no longer buys the whole top-up before a forecast solar day**
When a Charge By Time target is enabled in the evening, the deadline resolves to the following local day, so the optimizer plans across a runway that contains an entire forecast solar day. In that situation the plan committed the whole forecast-shortfall grid top-up in the first slots after the target was armed — often 15 to 18 hours before the deadline.

Under a flat pre-deadline import price that placement bought nothing. Moving the identical energy later produced the same planned cost and the same SOC at the deadline. Buying it overnight could not be undone: the SOC it purchased was exactly the headroom the next morning's solar then could not use, so surplus generation was exported at the feed-in rate instead of being stored.

PowerSync now holds the grid top-up until credited forecast solar can no longer meaningfully contribute before the deadline, and only then prefers the earliest slots. Each rolling re-solve can therefore shrink or cancel the planned import when solar over-delivers. The deadline itself is unchanged: the SOC floor, the solar prefill ceilings, and the reachability cap all behave exactly as before, the same total energy is planned, and a genuinely cheaper overnight price still wins — this only decides placement when the prices are tied.

**Deadline charging still completes with slack**
The earlier fix that stops a charge window drifting into a Happy Hour or peak boundary is preserved. When there is no credited forecast solar left in the runway, charging starts at the first available slot exactly as it did before, and each rolling solve flips back to that behaviour as the day's remaining solar shrinks.

Update available via HACS
