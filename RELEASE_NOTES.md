<!-- release: v2.12.1285 -->

## What's Changed

**Manual Tesla charging now follows the same connection status as the EV card**
When an identity-safe, current Wall Connector observation marks the selected Tesla connected, manual Solar Only, Limited Grid + Solar, and Full Grid + Solar starts now use that same readiness result instead of incorrectly reporting that the vehicle is not plugged in. Existing safeguards still reject away vehicles, ambiguous connectors, stale direct observations, and unmatched vehicle identities.

Update available via HACS
