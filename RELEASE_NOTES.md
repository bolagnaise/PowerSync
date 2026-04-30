## What's Changed

**Dashboard: Unified gauge styling across the price + battery cards**
The Battery gauge previously rendered with the stock Home Assistant gauge card while the Import and Export Price gauges used a custom SVG arc — three cards on the same row with two different visual treatments. They now all use the same custom SVG arc gauge, giving the dashboard a consistent look across the gauge row.

**Generalised the SVG gauge helper**
The internal `_priceGaugeCentsCard` helper has been renamed to `_svgArcGaugeCard` and reworked to take a config object with explicit `unit`, `multiplier`, and `decimals` fields. This is what made the battery gauge migration possible — battery percentage uses no multiplier and zero decimals, while the price gauges use a 100× multiplier (to convert $/kWh into c/kWh) and one decimal. The same helper can now be used for any future gauge regardless of unit, scale, or precision.

Update available via HACS
