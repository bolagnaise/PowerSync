<!-- release: v2.12.1092 -->

## What's Changed

**Tesla hardware reserve stability**
Fixed Smart Optimization repeatedly following a falling Powerwall SOC with lower self-consumption reserve writes. PowerSync still performs the initial current-SOC safety alignment that prevents an unintended grid charge, but now holds that optimizer-owned target for the rest of the session instead of ratcheting the physical reserve down on every cycle. Disabling and re-enabling Smart Optimization starts a fresh safe alignment, and failed mode or reserve commands cannot leave stale ownership behind.

Update available via HACS
