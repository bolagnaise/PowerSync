<!-- release: v2.12.1068 -->

## What's Changed

**Paired Tesla BLE bridges now remain one physical vehicle everywhere**

PowerSync now migrates saved Tesla BLE bridge profiles onto their paired Fleet
vehicle identity, so the mobile app's per-vehicle settings show one entry per
physical car instead of separate Fleet and BLE copies. The configured VIN
profile remains authoritative, while genuinely standalone BLE vehicles are
preserved.

Solar Surplus now evaluates only vehicle profiles whose per-vehicle Solar
Charging toggle is enabled. Paired Fleet and BLE identities are coalesced
before priority selection, preventing a disabled vehicle from being selected
through a stale bridge profile.

Dynamic charging start and stop operations also canonicalize paired BLE aliases
to the physical vehicle before claiming ownership or creating runtime state.
This prevents duplicate sessions, timers, and commands from controlling the
same car under separate identifiers.

Update available via HACS
