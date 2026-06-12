<!-- release: v2.12.624 -->

## What's Changed

**Fix optimizer fallback after infeasible LP solves**
PowerSync now preserves schedule timestamps when the LP optimizer falls back to the safe self-consumption hold path. This fixes a runtime error where an infeasible optimization could fail again instead of producing the protective hold schedule.

Update available via HACS
