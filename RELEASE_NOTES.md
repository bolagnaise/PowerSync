<!-- release: v2.12.1731 -->

## Consistent battery control status

The optimiser plan's **Current control** now follows the live request in **Battery controls**, instead of showing Self-consumption during an active discharge request. It also shows pending acknowledgement and restoration, and updates between plan calculations. A request remains separate from measured battery or grid power.

Update through HACS, restart Home Assistant, then refresh the dashboard.

## Clearer ongoing support setup

The Support card now explains recognized invitation problems, including an expired invitation, a different setup identity, an invalid public key or pending revocation. Follow the displayed next step and keep your current setup identity; reviewing an invitation still requires your separate approval before access begins.

## Automatic Tesla EV recovery

Solar Surplus no longer skips recovery when an unconfirmed Start leaves a possible charging current and vehicle telemetry is unavailable. It retries the existing recovery checks while preserving electrical protection, and unconfirmed load no longer counts as available solar.

A new PowerSync-owned charging session can also recover automatically when a completed session's old ownership lease was blocking the same verified native connection. Changed providers or entities, unresolved commands and pending cleanup remain protected.

Retained native Tesla command holds are also checked automatically after restart, even without an active charging session. Background checks use existing telemetry and add no provider requests or wake commands. Complete, advancing stopped/zero readings are still required; stale or conflicting data keeps the hold in place.

Update through HACS and restart Home Assistant. Automatic recovery status and the next retry are available through **PowerSync: Inspect Tesla EV recovery status**.
