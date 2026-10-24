<!-- release: v2.12.1490 -->
## What's Changed

**Today's actual electricity prices**
The PowerSync dashboard now shows a separate Actual Prices - Today graph alongside the TOU schedule. It displays recorded import and export prices from midnight to now, including earlier spikes and negative prices, with a labelled Now marker and blank future hours.

**Clearer tariff history and forecasts**
The TOU schedule now explains that it is a tariff model and may show tomorrow's model or repeated prices before the current time. The actual-price graph uses Home Assistant's timezone and available recorder history; missing past prices are not reconstructed from forecasts.

Update through HACS, restart Home Assistant, and refresh the PowerSync dashboard to load the graph.
