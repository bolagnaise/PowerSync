<!-- release: v2.12.370 -->

## What's Changed

**Neovolt stack capacity display**
The Neovolt configuration form now preserves the exact stack capacity text entered by the user. For single-integration systems, PowerSync can still sum `20.1, 30.2` into the effective runtime capacity it sends to the controller, but reopening the options form will keep showing the original stack values instead of rewriting the field to `50.3`.

**Clearer Neovolt surplus-balancer guidance**
The Neovolt surplus-balancer help text now explains why Auto mode can show as disabled when only one Neovolt integration is selected. Independent balancing needs multiple selected Neovolt integration entries so PowerSync can command each inverter separately; otherwise the runtime correctly treats the balancer as unavailable.

Update available via HACS
