# Recover retained EV charging headroom

An uncertain Tesla charging command can leave a conservative EV reservation
that blocks Powerwall battery grid charging. Previously, automatic recovery
could only wait for the provider's cached charge data to advance. A fresh log
does not make that data advance.

After updating through HACS and restarting Home Assistant, use **Developer Tools
→ Actions → PowerSync: Recover EV charging headroom** for the affected vehicle.
Enable **Allow provider refresh** to authorize this attempt. Teslemetry refreshes
can consume credits. For multiple PowerSync entries, specify the PowerSync
configuration entry ID.

```yaml
action: power_sync.recover_ev_headroom
data:
  vin: YOUR_17_CHARACTER_VIN
  allow_provider_refresh: true
```

The action requests up to two independent native vehicle charge reports and two
local Powerwall meter updates, with a five-second interval so normal two-second
meter polling can satisfy the unchanged three-second acquisition-progress check. Each round
has an eight-second read timeout. There is a one-minute cooldown per vehicle
after an attempted recovery. There are no automatic paid retries or background
changes. The car must be able to provide fresh data; the action does not wake it.

A successful response includes `success: true` and `reservation_cleared: true`.
This means the targeted reservation has been removed durably after verified
readback. It does not start battery charging: retry the intended charge action
or allow the optimizer's next cycle. Other battery or EV safety checks still
apply. Success does not establish historical command execution or actual battery
grid charging.

If a Scheduled Charging session has ended, its expired ownership lease no longer
blocks read-only recovery for the same verified native vehicle source. Unresolved
commands, changed identities and active owners still block recovery. This applies
to existing retained reservations after updating; no store edit is needed.

**PowerSync: Inspect Tesla EV recovery status** now includes `headroom_recovery`.
It reports the command route and read-only settlement route separately, whether
a sidecar hold exists, whether a matching reservation is loaded, and the exact
native-source selection verdict and matching-source count. It does not refresh
the vehicle or meter. `no_retained_history` describes the command journal; it does
not mean no headroom reservation exists. A `native_source_unavailable` response
also includes this projection under `preflight`; send that response if refused.

If the action returns `success: false`, keep the reservation and use the returned
`error` to choose the next step:

| Error | Meaning / next step |
|---|---|
| `provider_refresh_confirmation_required` | Enable Allow provider refresh for this attempt. |
| `reservation_not_found` | This VIN has no retained managed reservation; another guard may be blocking charging. |
| `reservation_scope_mismatch` | Check the VIN and PowerSync entry ID. |
| `native_provider_required` / `native_source_unavailable` | A uniquely identified, enabled Tesla Fleet or Teslemetry native source is required. BLE-only evidence cannot use this action. |
| `local_meter_unavailable` | The configured local Powerwall meter cannot be refreshed. |
| `site_busy` / `recovery_cooldown` | Let the current operation finish; wait one minute before another recovery attempt. |
| `recovery_scope_blocked` / `recovery_scope_changed` / `reservation_identity_changed` | Ownership, an unresolved recovery hold, or source/configuration changes prevent settlement. Resolve that cause; do not reset the store. |
| `fresh_readback_unavailable` | The provider/meter failed, timed out, or did not supply a usable fresh charge report. No wake is attempted. |
| `vehicle_not_stopped` | The charge report does not establish a stopped vehicle with zero current and power. |
| `settlement_not_confirmed` | The readings did not meet the existing timing/alignment and consistency checks. |
| `recovery_failed` | Recovery could not finish, including a possible persistence failure; retain the error response for support. |

No additional diagnostic log is required to try this action. If it cannot finish,
send the response and attempt time first; request further logs only for a specific
unanswered failure.

## Implementation and verification contract

The service targets one exact entry/VIN reservation, serializes with site
admission, and binds the retained producer identity to the matching native
vehicle source and local meter. It refuses active provider tasks, live/recovered
owners, native control claims, unsafe blocked routes, sidecar holds and runtime changes.
Authorization is rechecked around awaits and revoked when the attempt ends.
An otherwise valid native route whose only failed check is its ended ownership
lease may supply read-only stop proof when no command or hazard remains unresolved.
Its command route and historical journal remain unchanged.

Independent reads use the qualified concrete cloud reader, including explicit
Teslemetry refresh consent, without command-router calls, wake, or publishing
into the provider's cached stream. Existing reconciliation retains all identity,
freshness (15 seconds), alignment (5 seconds), acquisition progress (3 seconds)
and durable-save checks. It never directly deletes a reservation or recovery
journal. Other vehicles and entries remain untouched.

Regression coverage includes the retained 80 A Start restored after restart,
stale stream data with advancing independent reports, durable recovery, unrelated
reservations, absent consent, concurrency, ownership/provenance changes, stale or
contradictory readback, persistence failure, cancellation and bounded retries.
Publication and reporter confirmation remain separate from these source tests.
