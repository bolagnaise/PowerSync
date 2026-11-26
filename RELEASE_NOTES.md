<!-- release: v2.12.1846 -->

## New: Amber forecast accuracy card

Amber users have a new **Amber forecast accuracy** card on the PowerSync dashboard. It shows how many forecast export price spikes over the last 7 days actually happened, as a hit rate, and a 48-hour chart of the settled export price against the forecast made 1 or 4 hours earlier. Each forecast spike has a dot: green if it happened, red if it didn't.

Use it to decide on **Amber Forecast Confidence**. After 2 days and at least 3 forecast spikes, the card says whether the setting suits your results. If most spikes didn't happen, it may suit you; if most did, leaving it off lets the plan hold energy for them. The same figures are in the new **Amber Forecast Accuracy** sensor. Tracking starts when you update, so the card fills in over the first few days. It is information only and does not change how the optimizer plans.

Update through HACS and restart Home Assistant.
