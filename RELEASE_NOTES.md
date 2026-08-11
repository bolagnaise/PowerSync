<!-- release: v2.12.1074 -->

## What's Changed

**Correct Tesla UMC home-load accounting**
An idle Tesla Wall Connector reporting 0 W no longer hides charging power from a different, explicitly mapped Tesla using a UMC. PowerSync now subtracts that independently identified vehicle draw from home load while continuing to fail closed when vehicle identity is ambiguous.

**Exclude disabled EV schedules from the optimiser**
Smart Optimization no longer includes cached charging windows from a vehicle whose Smart Schedule is turned off. Auto-schedule status also reports the configured vehicle name and suppresses stale disabled decisions, so an active W3RT1E session is no longer presented as TESSY charging.

Update available via HACS
