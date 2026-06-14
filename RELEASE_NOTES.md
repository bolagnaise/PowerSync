<!-- release: v2.12.646 -->

## What's Changed

**Reduce repeated scheduled EV stop commands**

Scheduled Charging now remembers a recently sent external stop command for the same Tesla or charger and stop reason. If Tesla/Teslemetry briefly keeps reporting the vehicle as actively charging after PowerSync has already sent the stop command, PowerSync will not resend the same stop every evaluation cycle.

This reduces unnecessary Teslemetry command credit usage when PowerSync is holding a plug-and-play Tesla charger outside the allowed charging window.

Update available via HACS
