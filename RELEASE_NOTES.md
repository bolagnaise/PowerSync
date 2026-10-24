<!-- release: v2.12.1483 -->
## Powerwall Max discharge uses full site capability

Paired Tesla systems now use the previous tariff method for explicit Max discharge, after safely releasing local wattage control. This avoids restricting larger systems to the gateway’s timed-command battery ceiling. Numeric kW targets continue to use local control; site export limits and backup reserves still apply. Max charging already uses the native tariff method when eligible.

## Export targets account for home load

The Force Power export range now respects both the site export limit and the battery output available after home load and solar. For example, a 10 kW battery-output ceiling with 8 kW home load and no solar leaves about 2 kW for grid export. This range applies to precise local targets; Max uses the separate full-rate route when its safety checks allow it.
