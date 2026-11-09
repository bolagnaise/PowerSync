<!-- release: v2.12.1663 -->

## Fronius curtailment after a restart

Fronius simple-mode curtailment now retains its restore record across Home Assistant restarts. PowerSync checks the inverter identity and fresh registers before resuming normal curtailment and restore decisions, and shows recovery as pending until physical curtailment is verified.

Recovery requires a record saved before PowerSync changed normal output. Older disabled settings without that record and user power caps are left untouched. Update through HACS and restart Home Assistant.
