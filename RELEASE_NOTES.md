<!-- release: v2.12.1363 -->

## What's Changed

**Send negative electricity prices to Powerwall**
Tesla tariff sync now keeps negative import and export rates from Amber forecasts and live prices, AEMO wholesale pricing, and Flow Power after retail adjustments. A configured Powerwall accepted and returned negative buy and sell rates in a live test, and its original rate plan was restored. Tesla's own Savings mode decides how the battery responds to those prices.

**Keep signed prices visible in PowerSync**
The current price sensors, Flow Power plan pricing, and energy cost tracking now retain negative rates instead of showing zero. This keeps the displayed tariff and cost estimates aligned with the rates sent to Tesla. PowerSync Cloud's Tesla tariff sync has the matching update.

Update available via HACS
