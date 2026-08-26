<!-- release: v2.12.1197 -->

## What's Changed

**Demand Charge peaks now fail safely if Home Assistant cannot restore their
same-cycle stored value.**

PowerSync now logs Demand Charge peak restore and save failures at warning
level. If the saved peak cannot be read, or its current-cycle storage record
is structurally invalid, `Peak Demand This Cycle` and its dependent estimated
cost are shown as unavailable instead of incorrectly resetting to `0.0`.
PowerSync preserves the existing stored record rather than overwriting it with
the new coordinator's initial value, retries a transient restore on the next
update, and merges any recovered peak with samples observed in the session.

Normal first use, a new billing cycle, and an intentionally changed Demand
Charge window continue to start a fresh peak. This accounting-only correction
does not issue battery, EV, inverter, or other hardware commands.

Update available via HACS
