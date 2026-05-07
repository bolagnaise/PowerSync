<!-- release: v2.12.315 -->

## What's Changed

**Solax AC inverter model selection**
Selecting Solax for AC-coupled inverter curtailment now shows Solax model families instead of falling back to the Sungrow model list. The shared model lookup now has explicit options and connection defaults for Solax, Sigenergy, and AlphaESS, so the setup dialog matches the selected brand.

**ZeroHero free import charging forecast**
Smart Optimization now keeps force charge active for the whole free-import window instead of stopping the displayed charge plan once the projected SOC reaches full. This keeps the forecast and action list aligned with the intended command behavior while still respecting charge-block periods.

Update available via HACS
