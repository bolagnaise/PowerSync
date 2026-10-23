<!-- release: v2.12.1474 -->
## Powerwall solar permission on the dashboard

Paired Tesla systems now show **Allow on-grid solar curtailment** in the Powerwall Local Control dashboard card. The switch reads the saved gateway permission; combine it with a zero/limited-export policy. It does not immediately shut down PV or establish compatibility with a separate AC-coupled inverter.

## Powerwall calibration delay

New actions read the gateway's calibration-delay allowance and request a delay in whole hours. The delay selector is enabled only when the gateway reports an available allowance; firmware returning an unimplemented status remains unavailable. Requests are not automatically retried, and the existing calibration alert and optimiser protection remain active.
