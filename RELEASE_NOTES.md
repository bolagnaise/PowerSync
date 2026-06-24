<!-- release: v2.12.707 -->

## What's Changed

**Charge By Time infeasible solve guard**
PowerSync now caps Charge By Time pre-window SOC targets using the same charge availability rules the LP optimizer will actually enforce. This prevents a near-term 100% target from making the optimizer infeasible when charging is blocked before the target window by export/profit rules, especially when Auto-Apply Optimizer Reserve is enabled from a low battery SoC.

**Optimizer reserve stability**
The optimizer keeps the safe self-consumption fallback behavior for genuinely infeasible solves, but avoids creating the false infeasible condition caused by an impossible pre-window reserve deadline. This should stop Auto-Apply Optimizer Reserve from repeatedly falling back to self-consumption immediately after being enabled in the affected Charge By Time scenario.

Update available via HACS
