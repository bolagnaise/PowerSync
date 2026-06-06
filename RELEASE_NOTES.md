<!-- release: v2.12.606 -->

## What's Changed

**GoodWe force-discharge export release**
GoodWe systems now release PowerSync's zero-export curtailment before user or optimiser force-discharge commands are sent. This prevents a 0W curtailment limit from blocking export during profitable discharge windows, and stops the curtailment loop from immediately reapplying the limit while force export is active.

**Sungrow discharge-limit restore**
Sungrow systems now remember the previous discharge-rate limit before temporary force-discharge or no-discharge control, then restore that limit when normal operation resumes. This avoids leaving the inverter at a temporary cap after a scheduled battery-control action finishes.

**Timestamped export-price sensors**
The optimiser now understands timestamp-keyed export price values from EPEX-style Home Assistant sensors, including both `price_values` maps and direct timestamp attributes. This lets export-price forecasts align to optimiser slots instead of falling back to a single static sensor value.

Update available via HACS
