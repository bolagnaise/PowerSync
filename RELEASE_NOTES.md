<!-- release: v2.12.1713 -->

## Simpler Tesla site limits

Site limits now sit in Controls → Advanced options, alongside the other advanced Powerwall controls. Adjust persistent import and export limits directly, without an approval-document form. Existing gateway readback and restore protections remain in place.

## Idle and battery percentage targets

Paired V1R gateways can hold the battery at exactly zero watts or charge/discharge to a chosen percentage, with a maximum duration. The optimiser uses zero-power control for true idle slots when V1R is selected; preserve-charge behaviour still allows charging. Existing Force controls keep their current behaviour.

## Backup preparation

A new advanced control prepares a timed native backup window while keeping the house connected to the grid. It respects charging permission and site limits, provides recovery checks for interrupted requests, and leaves backup windows created elsewhere untouched.

Update through HACS and restart Home Assistant. These features require a paired, compatible Tesla gateway. PowerSync supervises percentage targets using telemetry; support and physical behaviour depend on gateway firmware and installation.
