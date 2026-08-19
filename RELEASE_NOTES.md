<!-- release: v2.12.1151 -->

## Fixes three crashes: Octopus setup, Tesla setup, and the optimizer's safety fallback

Three code paths referenced names that were never defined. Each raises the moment its branch runs — there is no partial or degraded behaviour, the operation simply fails. All three have been present for a long time and are unrelated to any recent change.

**Update if you use Octopus Energy UK, or configured your battery through discovered Home Assistant entities.** Everyone should update for the optimizer fix.

### Octopus Energy UK: the integration could fail to load

If PowerSync had no export product code stored for your account, it fell through to a default lookup that referenced two constants the module never imported. That raised during setup, so the entire integration failed to start — not just the export tariff.

Anyone whose export tariff was already stored, or was discovered from their Octopus account, never reached that fallback. That is why this survived so long: it only bites accounts set up without an export tariff.

### Tesla on a discovered-entity battery profile: the integration could fail to load

When PowerSync builds a battery coordinator from discovered Home Assistant entities, it checks which brand those entities belong to. The Tesla branch of that check compared against a constant that was never imported, so setup crashed for Tesla and Powerwall systems configured through the discovered-entities route.

Sigenergy, Sungrow, AlphaESS and GoodWe are all tested earlier in the same chain and were unaffected.

### Optimizer: the safety fallback crashed instead of catching

When PowerSync plans around manual battery modes, it re-solves several times so the plan matches what the hardware will actually do. If that projection does not settle and no usable plan survives, it falls back to a simpler heuristic solver.

That fallback passed three of its arguments under the *receiving* function's parameter names rather than its own, so the moment it was reached it raised — turning the mechanism that exists to catch a failure into a second failure. It now passes the correct values, matching the two other places that call the same solver.

Most sites will never have reached this path: it needs the projection to not converge *and* no retained plan to fall back on. But a fallback that crashes is worse than no fallback, and this one sits directly in the optimization loop.

### Stopping this class of bug recurring

All three were invisible to normal checks. Every module imports cleanly; the bad line only runs when its specific branch does, which for the optimizer fallback means the moment the solver gives up.

PowerSync now has a test that walks every file in the integration and fails if any name is used that is not defined in an enclosing scope. It found exactly these references and nothing else, and it runs in under three seconds on every future change. The same check would have caught the Sigenergy vehicle-list crash fixed in 2.12.1150.

A smaller fix also landed in the tariff converter, where two type annotations referenced an unimported name. Those annotations are never evaluated at runtime, so nothing was broken — but they are now correct.
