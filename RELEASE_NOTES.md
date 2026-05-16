<!-- release: v2.12.412 -->

## What's Changed

**Choose the Solcast estimate PowerSync uses**
Weather and solar settings now include a Solcast estimate selector, so systems that track closer to `estimate10` can have PowerSync sensors and Smart Optimization use that conservative forecast instead of the default estimate. The selected estimate is applied across both the built-in Solcast coordinator and the optimizer forecast reader.

**Preserve custom dashboard layouts**
PowerSync no longer overwrites a user-managed Lovelace dashboard on reload, and saved dashboard card layouts are reconciled when cards are added, removed, or renamed. Existing custom layouts should survive upgrades while still allowing empty legacy dashboards to initialize correctly.

**Improve inverter and EV control edge cases**
GoodWe systems now reapply self-consumption when they are still discharging into grid export, Fronius Reserva force-charge writes are attempted even if the power entity is slow to become available after a mode switch, and Tesla Wall Connector power is counted before probing vehicle sensors for solar-surplus EV decisions.

Update available via HACS
