<!-- release: v2.12.836 -->

## What's Changed

### Reliable Sungrow spread-export recovery

Sungrow systems now preserve their normal grid-export limit before Smart Optimization applies a temporary spread-export target. If Home Assistant or the integration reloads during the export window, PowerSync restores the original enabled limit—or the original disabled state—instead of treating the temporary lower target as the permanent baseline. Control writes are also withheld if the recovery state cannot be saved safely.

### Refreshed spread-export targets

Active spread-export windows can now increase to a newly calculated optimizer target instead of remaining pinned to an older lower value. Refreshed commands continue to respect the configured maximum battery-discharge and grid-export limits.

Update available via HACS
