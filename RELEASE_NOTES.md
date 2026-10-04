<!-- release: v2.12.1350 -->

## What's Changed

**Generic EV charging accommodates delayed cloud feedback**

Generic managed starts, including Price-Level charging, now wait up to 60 seconds after the control service returns for fresh charging-status or measured-power feedback. Cloud chargers that take around 45 seconds to report charging are no longer stopped after just five seconds. An accepted command or an on switch still cannot create a managed session, start notification, or home-battery preserve request without fresh confirmation.

**Pending starts remain safe to cancel and hand over**

A stop request during confirmation prevents the pending start from becoming a session. Timeout or cancellation attempts a compensating stop, and an ownership change during confirmation cannot overwrite another mode's lease. Telemetry received during wake or current setup cannot confirm a later switch-start command. Existing disconnected-EV and current-limit safeguards remain in place.

Update available via HACS
