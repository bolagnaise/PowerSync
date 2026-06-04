<!-- release: v2.12.568 -->

## What's Changed

**GloBird account and usage sensors inside PowerSync**
GloBird users can now connect their GloBird portal account during setup or from options. PowerSync creates a linked `GloBird Pricing` device with account balance, invoice, service and meter status, latest data readiness, usage/export totals, latest-day cost, billing-period metrics, ZeroHero status, expected monthly cost, and weather-impact summaries. These sensors are additive to the existing GloBird tariff and AEMO spike behaviour, so optimiser import/export price sensors continue to work as before.

**Flow Power portal login during initial setup**
Flow Power setup now supports signing into the Flow Power portal with email, password, and SMS verification during the initial config flow, not only through options re-authentication. Flow Power pricing and portal account sensors now sit under a linked `Flow Power Pricing` device, including import/export price, TWAP, network tariff, PEA, LWAP, DLF, and demand metrics when portal data is available.

**Provider pricing cards in the Home Assistant dashboard**
The generated PowerSync dashboard now adds hideable provider-pricing cards when GloBird or Flow Power account entities exist. GloBird cards surface readiness, latest usage/export/cost, balance, invoice, ZeroHero, and billing-period metrics; Flow Power cards include the existing price sensors plus portal account metrics such as PEA, LWAP, TWAP, DLF, and demand.

Update available via HACS
