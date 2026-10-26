<!-- release: v2.12.1511 -->

## Improved

### Powerwall charging and export diagnostics

When native Max charging or export cannot start, PowerSync now logs a more specific reason and relevant limits or readings. This helps identify missing gateway data, export-limit restrictions, reserve checks, stale readings and control ownership without logging credentials or ownership tokens.

This update improves diagnosis; it does not bypass safety checks or establish a fix for every failed export. Update through HACS and restart Home Assistant, then include the admission-denied log line if the next intended charging or export window fails.
