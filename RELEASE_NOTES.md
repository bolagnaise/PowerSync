<!-- release: v2.12.865 -->

## Monitoring Mode handoff safety

- Monitoring Mode now performs one complete control handoff only when it changes from disabled to enabled, including release of active force control and restoration of any temporary IDLE/EV backup reserve.
- Saving provider settings or turning Monitoring Mode on again while it is already enabled is now write-free.
- If the control cleanup cannot complete safely, Monitoring Mode remains disabled and reports the failure instead of persisting a partial transition.
