<!-- release: v2.12.1090 -->

## What's Changed

**Choose one battery connection owner and avoid competing local clients**

PowerSync now exposes a brand-specific **Connection method** for every supported battery system. Existing installations retain their current route automatically, while users can explicitly choose a PowerSync direct connection, an existing Home Assistant battery integration, or a monitoring-only integration profile where safe control parity is not available.

Home Assistant-backed profiles are resolved before battery coordinators are constructed, reuse the selected upstream config entry, and never fall back to a second direct Modbus or local API client. Monitoring-only profiles block optimizer and manual writes. Switching profiles first restores active force, idle, reserve, or curtailment state through the old route and leaves the old profile selected if cleanup cannot be confirmed.

The new integration sensor catalog discovers additional battery, solar, grid, load, energy, inverter, charger, and diagnostic entities only inside the selected upstream integration scope. Recommended and All supported display modes add those original entities to the PowerSync dashboard without cloning their recorder history or inferring controls from sensors.

Included connection profiles cover Tesla, Sigenergy, Sungrow, FoxESS, GoodWe, AlphaESS, ESY Sunhome, SolaX, SAJ H2, Fronius GEN24/Reserva, Neovolt/Bytewatt, SolarEdge, Anker Solix, and custom/external systems. GoodWe can use validated HA telemetry plus EMS controls or a monitoring-only HA route; SolarEdge HA-only mode disables direct curtailment; and Sungrow, Sigenergy, Tesla Powerwall, and AlphaESS have explicit monitoring-only HA choices.

Update available via HACS
