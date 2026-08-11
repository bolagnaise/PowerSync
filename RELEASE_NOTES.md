<!-- release: v2.12.1077 -->

## What's Changed

**All smart EV modes now leave away Teslas under local charger control**
When Home Assistant reports a Tesla outside the home zone, Solar Surplus, Price-Level Charging, Scheduled Charging, Smart Schedule, and the legacy EV optimizer now release their PowerSync timer, session, and ownership state without sending a start, stop, or charging-current command. Charging started by the driver or a remote charger therefore continues uninterrupted.

**Away protection is enforced at every automated command boundary**
Solar Surplus no longer selects an away vehicle merely because remote charging makes it appear plugged in. Active single- and multi-vehicle timers, tracked and untracked session cleanup, mode disable paths, and physical command dispatch all recheck the Tesla location. Concurrent session replacement is also protected so stale cleanup cannot remove or control a newer session.

At-home Tesla behavior and fixed home chargers such as OCPP, Zaptec, Sigenergy, and generic charger entities are unchanged.

Update available via HACS
