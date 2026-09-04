<!-- release: v2.12.1234 -->

## What's Changed

**Tesla hardware backup reserve remains authoritative during Self Consumption**
When a Tesla battery is below its configured Hardware Backup Reserve, PowerSync
now keeps that configured reserve instead of lowering it to the current state
of charge during ordinary Self Consumption. This preserves the selected outage
floor across first-run, mode-transition, and restart paths; confirmed Tesla
readback and temporary-control recovery safeguards remain in place.

**Sigenergy Solar Surplus charging can release zero-export headroom**
When Sigenergy DC curtailment is active because export is uneconomic, an active
internally owned Solar Surplus EV session now temporarily restores the saved
export limit so the EV can discover and use available PV. Zero export is
reapplied when that EV session no longer qualifies, while existing price,
connection, ownership, pause, SOC, and controller-confirmation safeguards are
retained.

Update available via HACS
