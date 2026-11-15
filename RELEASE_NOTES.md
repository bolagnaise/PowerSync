<!-- release: v2.12.1708 -->

## Faster Home Assistant startup

PowerSync no longer holds up Home Assistant startup while network export checks wait for a slow optimization run. These checks continue in the background, with existing export safety checks retained.

Update through HACS and restart Home Assistant. This addresses the startup delay; it does not establish that a separate solar-storage planning concern is resolved.
