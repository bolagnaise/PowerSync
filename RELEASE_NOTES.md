<!-- release: v2.12.1558 -->

## What's Changed

### Monitoring Mode reserve cleanup

Enabling Monitoring Mode can now restore the backup reserve temporarily raised by the optimiser instead of blocking its own cleanup. If Tesla cannot confirm the restore, Monitoring Mode still stays off and keeps the saved reserve for retry.

Update through HACS, restart Home Assistant and try enabling Monitoring Mode again. Teslemetry credit exhaustion is separate and can still prevent cloud EV commands.
