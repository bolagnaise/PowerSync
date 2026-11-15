<!-- release: v2.12.1715 -->

## Manual Tesla controls in Monitoring Mode

Paired V1R battery power, Idle, percentage targets, site limits and backup preparation can now be used manually while Monitoring Mode is on. Automatic actions remain blocked, and pairing, live readings, ownership and recovery checks still apply.

## Clearer battery controls and limits

Fixed power, Idle and charge/discharge-to-% are directly on the dashboard Controls screen, with site limits and backup preparation under More controls. Sections load independently, retain edits and stay open during confirmation; failed reads show a reason and can be retried.

The controls and wiki explain watts, whole-site totals and why the inverter or gateway may reject a value within the input range. Update through HACS and restart Home Assistant; reload the dashboard to load the updated controls.
