<!-- release: v2.12.1059 -->

## What's Changed

**Profit Max solar export now supports additional battery control systems**

Profit Max can now use a verified, temporary charge-only hold on Sigenergy,
Sungrow SH, FoxESS, SolaX, Fronius Reserva/GEN24, and Neovolt systems. Sungrow
dual-inverter installations and the separate FoxESS Modbus, Home Assistant
entity, and Cloud control paths are handled as distinct control targets. The
feature remains built into Profit Max; there is no additional switch.

The control lifecycle is now provider-neutral and fail-closed. Before changing
hardware, PowerSync persists the exact adapter version, every battery/inverter
target, and each target's current normal value. Solar Export is reported only
after every target verifies the charge block. Consecutive export slots retain
the original restore value, and the v2.12.1058 Sigenergy lifecycle state is
migrated safely after an update or restart.

If capability discovery, preparation, a write, verification, or restoration is
unavailable or fails, PowerSync cancels the Solar Export action, attempts normal
restoration on every target, and executes ordinary self-consumption. Incomplete
cleanup remains persisted for retry and prevents another hold. Tesla, GoodWe,
AlphaESS, ESY Sunhome, SAJ H2, SolarEdge, Anker Solix, Custom controllers, and
unsupported variants use this explicit normal-control fallback because their
current PowerSync control surfaces do not provide a proven reversible,
observable charge-only primitive.

Profit Max economics, Charge By Time priority, monitoring mode, provider charge
blocks, export curtailment ownership, existing settings, and API/mobile action
compatibility are unchanged. Capability, multi-target compensation, migration,
normal fallback, optimizer policy, and adjacent provider paths are covered by
Python 3.12 regression tests. Physical hardware canary testing was not performed
for every supported control path.

Update available via HACS
