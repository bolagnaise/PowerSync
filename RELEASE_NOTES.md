<!-- release: v2.12.1060 -->

## What's Changed

**Fronius Profit Max holds now preserve Battery API Mode**

Profit Max Solar Export on Fronius Reserva/GEN24 systems now uses the independent
Storage Control Mode `Block Charging` command without changing the inverter's
persistent Battery API Mode. Both `Auto` and intentionally configured `Manual`
API-mode settings remain untouched while PowerSync applies and clears the
temporary charge hold.

Capability discovery now depends only on the storage control entity that the
hold actually uses. Storage mode is still captured before the command, verified
as `Block Charging`, restored to its exact normal value, and read back after
cleanup. Fronius force-charge, force-discharge, and idle controls are unchanged.

Version 2.12.1059 did not record the previous Battery API Mode before changing
it, so this update deliberately does not guess or rewrite that missing historical
value. Anyone who used Fronius Solar Export while running 2.12.1059 should check
Battery API Mode once and return it to their preferred setting if needed.

The Fronius controller, provider-neutral hold lifecycle, capability checks, and
adjacent optimizer paths are covered by Python 3.12 regression tests. Physical
Fronius hardware canary testing was not performed for this release.

Update available via HACS
