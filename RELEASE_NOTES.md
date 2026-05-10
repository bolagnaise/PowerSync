<!-- release: v2.12.365 -->

## What's Changed

**Neovolt mobile controls no longer expose Tesla-only modes**
Neovolt systems now report Powerwall settings as unsupported to the mobile settings endpoint, preventing Tesla-specific operation mode controls from appearing for Neovolt installs.

**Neovolt MAX force discharge now targets every inverter**
Fleet force discharge requests at the combined inverter limit now command each Neovolt inverter at its own maximum discharge power. Hardware refreshes for active Neovolt force discharge also preserve the original restore modes so extended runs do not overwrite the pre-force baseline.

Update available via HACS
