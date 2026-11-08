# EV command recovery redesign

## Defects to change

1. Native command uncertainty currently becomes an indefinite positive-control lockout. The required original-command terminal proof is unavailable through ordinary Fleet installations.
2. Start, Stop, current and limit uncertainty share a blanket admission hold despite different effects.
3. BLE protocol-2 recovery rejects Start/current/limit records even after the producer has rebooted into its read-only recovery mode and obtained advancing stopped/zero vehicle samples.
4. Finite manual control checks its producer binding before attempting native recovery, so it cannot reach a usable recovery path.

## Implementation

- Keep immutable historical outcomes and original request values. Record operational reconciliation separately using exact attempt IDs and source-linked observations.
- Native recovery requires a loaded valid journal, exact current producer, no live provider submissions, unchanged configuration/owner/generation and two advancing stopped/zero observations with stable current/limit settings. Wait at least 60 seconds after the last uncertain submission (120 seconds for Teslemetry); this is a settling policy, not a Tesla queue expiry guarantee. Bound the attempt and back off failed reads. Use exact provider refresh/read paths, including Tessie's source-state read instead of its ordinary cache; never invoke charging or wake commands from native reconciliation.
- Run reconciliation before native command selection, including finite manual binding capture. Subsequent Start/current/limit writes still use ordinary ownership and electrical admission. Keep Stop available and make it interrupt reconciliation.
- Extend the existing BLE protocol-2 whole-inventory transaction to all four charging operations. Preserve the Stop-only compatibility action and add a general action. A bridge reboot alone remains insufficient: all declared vehicles must provide fresh native stopped/zero receipts before durable release and firmware commit.
- Persist bridge commit-pending state across HA restart and expose it in both recovery status actions.
- Provide the protocol-2 producer adapter for yoziru as well as Pedro, with matching library request-correlation support and immutable source pins.
- Settle only the recovered car's captured reservation through the existing shared allocator and fresh meter/vehicle samples; preserve other cars' demand.
- Persist read backoff and show historical uncertainty separately from current admission blockers.
- Preserve Monitoring Mode, external ownership, VIN/config/device/unique-ID checks, site and phase budgets, persistence failures, generation fencing, deadlines and command confirmation.

## Acceptance

Replay the six native unknowns (including missing legacy Rate value) through recovery and then new Start/current/Stop admission, retaining all six historical outcomes across reload. Exercise changed owner/producer, stale or conflicting telemetry, active submissions, cancellation, save failure, new unknowns and multiple vehicles. Exercise BLE mixed operations with the same all-or-nothing inventory transaction. Run focused and adjacent suites before the mandatory full release gate.

This is operational convergence under Tesla's documented stateful-command model, not proof that a historical command never executed. Physical installation and vehicle verification remain separate from source tests. Development does not issue live controls.
