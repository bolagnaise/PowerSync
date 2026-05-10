<!-- release: v2.12.371 -->

## What's Changed

**Neovolt surplus balancer no longer idles stacks without surplus**
The Neovolt surplus balancer now only parks a higher-SOC stack when there is real solar/export context or active battery-fighting to resolve. If the site is not exporting and the system should naturally discharge, PowerSync will leave both Neovolt stacks in their normal dispatch mode instead of switching the higher-SOC stack to idle. If a stack was already parked and the surplus disappears, PowerSync restores it to normal.

**Force mode power survives Home Assistant restarts**
Manual and optimizer force-charge/force-discharge commands now persist their requested power setpoint as well as duration. If Home Assistant restarts while a force mode is active, PowerSync reissues the command with the original watt target instead of falling back to a default-power restart command.

Update available via HACS
