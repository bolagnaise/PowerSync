<!-- release: v2.12.1125 -->

## Per-phase EV load management

- Adds optional L1, L2, and L3 mains-current protection for PowerSync-managed EV charging.
- Coordinates multiple managed loadpoints against shared per-phase capacity instead of allowing each charger to consume the same headroom independently.
- Reduces or blocks managed charging when phase readings are missing, stale, invalid, or over the configured limit, while leaving manual charging ownership unchanged.
- Exposes Home Power configuration and live allocation status through the Home Assistant API for the mobile app.
- Adds focused regression coverage for allocation, stale-sensor fail-closed behavior, ownership boundaries, and OCPP command handling.
