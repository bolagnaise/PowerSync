<!-- release: v2.12.303 -->

## What's Changed

**GoodWe LAN / Kit-20 entity mode is now obvious**
GoodWe users with LAN or WiFiLAN Kit-20 modules can now configure PowerSync for TCP/502 plus the GoodWe Experimental Home Assistant integration's EMS entities. The setup flow labels the entity mode prefix clearly, validates the required EMS mode and power limit entities, defaults TCP setups to port 502, and points UDP failures toward entity mode instead of leaving users stuck on port 8899 troubleshooting.

**GoodWe setup documentation added**
The README and new GoodWe wiki page now explain the difference between direct UDP control and TCP/502 entity mode, including the exact prefix format for entities such as `select.goodwe_ems_mode` and `number.goodwe_ems_power_limit`.

**Sigenergy tariff sync restored during Smart Optimization**
PowerSync now keeps syncing tariff data to Sigenergy Cloud for app visibility while Smart Optimization owns dispatch locally through Modbus / Remote EMS. This restores the tariff graph without handing scheduling control back to Sigenergy native AI mode.

**Optimizer grid-charging controls are now generic**
The LP optimizer's grid-charging guard is now handled through a brand-neutral setting, with tests covering export/discharge behavior. This reduces brand-specific assumptions and makes optimizer behavior more consistent across supported inverter integrations. Export-profitable slots now also block same-slot grid charging, so grid energy is charged before export windows rather than being passed through during them.

**SAJ H2 force-mode verification improved**
SAJ H2 force charge/discharge now verifies engagement through the inverter working mode instead of relying on brittle voltage readings that can report zero while dispatch is active. This avoids false failure reports on systems where the inverter is actually responding.

**Optimizer mode switching simplified**
The optimizer mode hysteresis layer has been removed so dispatch decisions follow the current optimization plan more directly, with updated tests for export-allowed slots.

Update available via HACS
