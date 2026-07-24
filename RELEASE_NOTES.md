<!-- release: v2.12.925 -->

# PowerSync v2.12.925

## Fixed

- Corrected EV loadpoint source labels to use the actual deduplicated total EV load instead of treating EV demand as zero.
- Prevented configured generic HACS OCPP chargers from appearing as duplicate loadpoints, while preserving mapped power and current readings.
- Migrated legacy Smart Schedule `_default` entries to their configured charger identity and backend.
- Added bounded retry backoff for failed Smart Schedule start commands so unavailable chargers are not retried every evaluation cycle.
