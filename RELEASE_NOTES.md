<!-- release: v2.12.1097 -->

## What's Changed

**Show why Profit Max did or did not select Solar Export**

Fixed a diagnostic gap in the PowerSync Current Action sensor that hid the Profit Max solar-export capability and selection result. The sensor now exposes Monitoring Mode, Profit Max state, planned and effective action details, charge-hold status, and the current slot's exact selection or rejection reason.

**Report forecast-funding evidence for the current slot**

Solar Export diagnostics now include current solar surplus, effective export price, deferred battery energy, eligible cheaper replenishment energy, the cheapest replenishment cost, and compact rejection counts. This makes it possible to distinguish a capability or export-limit block from insufficient cheaper forecast replenishment without changing optimizer or hardware-control behavior.

Update available via HACS
