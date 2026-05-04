<!-- release: v2.12.282 -->

## What's Changed

**Restore the auto-created dashboard scene**
The PowerSync dashboard now keeps the built-in home energy-flow image visible after frontend updates. The generated dashboard explicitly provides the bundled scene asset path, the energy-flow card now supports SVG `href` and `xlink:href`, and a CSS background fallback keeps the scene visible even when a browser or Home Assistant frontend build is picky about SVG image loading.

**Keep dashboard sections from disappearing at startup**
The dynamic dashboard strategy now builds cards from existing entity metadata even when states are temporarily `unknown` or `unavailable`. Tesla and Powerwall controls are also detected across `power_sync_tesla_*`, legacy `power_sync_*`, and Home Assistant device-composed entity IDs, so the auto-created `/power-sync/energy` view should retain the expected controls and charts after restart or upgrade.

**Guard optimizer battery exports**
The optimizer now blocks unintended battery-to-grid export outside explicitly allowed export windows while still allowing solar surplus export. This prevents the LP solver from choosing impossible import-to-export passthrough or battery export arbitrage during normal cost optimisation.

Update available via HACS
