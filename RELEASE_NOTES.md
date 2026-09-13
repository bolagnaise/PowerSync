<!-- release: v2.12.1284 -->

## What's Changed

**Sigenergy optimizer exports preserve available solar**
When a Sigenergy optimizer export target can be met with live PV, PowerSync now uses the PV-preserving Remote EMS mode instead of selecting ESS-first solely because the plan includes a battery contribution. This prevents the inverter mode from suppressing solar and turning a planned export into grid import.

**Safer optimizer control when PV telemetry is unavailable**
Battery-target export commands now require fresh PV status before selecting a potentially PV-suppressing mode. If that status cannot be read, PowerSync refuses the optimizer command without changing the inverter registers; manual and non-optimizer commands retain their existing PV-first fallback.

Update available via HACS
