<!-- release: v2.12.1646 -->

## Use forecast solar before unnecessary grid top-ups

When Smart Optimization uses its fallback planner, it now considers cheaper available solar before buying extra grid energy and avoids purchases that would displace solar later. This prevents early paid charging in sunny-day plans while preserving Charge By Time targets, battery reserves and useful grid charging when solar cannot cover the need.

Update through HACS and restart Home Assistant. This changes battery planning; Solar Surplus EV charging still follows its battery minimum and live-surplus safeguards.
