<!-- release: v2.12.291 -->

## What's Changed

**Neovolt/Bytewatt battery support**
PowerSync can now connect to Neovolt/Bytewatt systems through the Neovolt Modbus HACS integration. The new bridge discovers the selected Neovolt config entry, prefers combined host/follower battery, load, PV, capacity, and SOC sensors when available, and falls back to host sensors for single-inverter installs.

**Smart Optimization dispatch controls**
Neovolt systems now support PowerSync force charge, force discharge, restore normal, and backup-reserve writes by driving the upstream dispatch power, duration, target SOC, cutoff SOC, and dispatch mode entities. Dynamic Export and Dynamic Import remain outside PowerSync control for this first release while the optimiser continues issuing scheduled dispatch actions.

Update available via HACS
