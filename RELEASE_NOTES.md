<!-- release: v2.12.1513 -->

## Fixed

### Powerwall restore status

When native Max settings have been restored but PowerSync is still checking that battery power has settled, the optimiser now reports a pending restore instead of a failure and Python traceback. The persistence log also explains that native Max recovery uses its own journal.

Ownership and retries remain active until physical verification completes; actual restore failures still report errors. This changes reporting, not battery ramp-down timing. Update through HACS and restart Home Assistant.
