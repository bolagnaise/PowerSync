<!-- release: v2.12.300 -->

## What's Changed

**Powerwall battery labels in Home Assistant**
The Battery Health sensor now publishes the reconciled pack label for each Powerwall pack, so the Home Assistant attributes expose the same role-aware names used by the per-pack sensors.

**Generated dashboard uses reconciled pack roles**
The generated PowerSync dashboard now reads the backend pack labels and roles before falling back to legacy follower/expansion flags. PW2 systems should show Powerwall 1/2/3/4 instead of misleading Leader/Follower labels, and PW3 systems should keep Leader, Follower, and Expansion labels aligned with the corrected detection logic.

Update available via HACS
