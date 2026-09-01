<!-- release: v2.12.1220 -->

## What's Changed

**EV charging decisions now ignore stale power telemetry**

Solar Surplus and Tesla BLE plug detection now use the same 90-second freshness boundary as Home Load attribution. A stale EV power reading can no longer be treated as current charging load for surplus allocation or connection inference, while fresh charger and Wall Connector readings continue to be used normally.

**Price-Level projections distinguish optimizer control from manual force mode**

An optimizer-owned home-battery force charge or discharge no longer labels Price-Level EV opportunity windows as blocked by “Manual force … is active.” Actual manual and external force modes remain visible as manual-force blocks. This change corrects the advisory projection only; it does not alter the site-import limit or the dynamic charger safety controls.

Update available via HACS
