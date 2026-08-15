<!-- release: v2.12.1105 -->

## What's Changed

**Restored quota-safe CovaU free-window charging**
PowerSync now schedules chronological whole charge slots for external and fixed-rate battery profiles whenever the remaining daily free-import allowance can safely cover them after reserving forecast home demand. A partially available CovaU allowance no longer removes the entire 0c window from the Action Plan.

**Preserved capped-allowance protection across tariff days**
Each daily quota group in the 48-hour horizon is budgeted independently. PowerSync keeps the earliest full-rate slots that fit and blocks only the remaining slots that could exceed the allowance, without pretending that fixed-rate hardware can accept a fractional power target.

Update available via HACS
