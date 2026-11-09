<!-- release: v2.12.1670 -->

## Solar Surplus EV charging during negative export prices

Sigenergy and Sungrow could keep exporting at negative prices while a plugged-in EV was not charging. Solar discovery is now time-limited, with export curtailment reapplied when increased charging is observed or the trial ends. A brief trial can still expose exports while PowerSync checks available solar.

This also fixes native curtailment status being hidden by a normal separate AC inverter and prevents stopped or replaced EV sessions from authorizing an export-limit release. Idle solar-load trials use valid, recent telemetry and are kept separate from measured surplus.

Update through HACS and restart Home Assistant. Solar Surplus still follows available solar; a negative import price alone does not authorize grid charging in this mode.

## Include a separate solar array

Under Configure → Advanced settings → Additional solar accounting, you can now include a separate array missing from the primary Solar reading. Select its registered Home Assistant power sensor and choose whether Home Load is measured directly or needs to be calculated from the whole-site power balance.

This is off by default. Enable it only for generation that is not already counted; missing or stale readings make the combined Solar reading unavailable, and earlier production is not reconstructed.
