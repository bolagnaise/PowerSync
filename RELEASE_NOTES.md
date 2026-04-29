## What's Changed

**Two-press confirmation on Unpair Powerwall Gateway**
Pressing **Unpair Powerwall Gateway** once now posts a confirmation notification ("Press again within 30s to confirm") instead of immediately wiping the RSA key. A second press within the 30s window does the unpair; outside it, the counter resets. Stops a single accidental tap (or stray automation) from forcing a re-pair, which requires physical access to the DC isolator. The Pair button is unchanged — re-pairing is recoverable, single-press unpairing wasn't.

Update available via HACS
