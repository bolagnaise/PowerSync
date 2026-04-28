## What's Changed

**SAJ H2: Cap force charge target at 100% rated to prevent grid-side protection trip**
The previous default sent `1100` to `passive_bat_charge_power_input` — stanus74's "no explicit limit" sentinel that lets the inverter exceed its AC nameplate. Field testing on an H2-8K showed this trips the inverter's grid-side overload protection in ~90 seconds: the inverter happily pushes charge target + home load past its 8 kW AC rating, then disengages itself to working_mode 4 with no fault reported, and only a power-cycle brings it back. The default sentinel is now `1000` = exactly 100% of rated capacity, so the inverter self-balances charge against load and stays within the AC rating — sustained charging instead of one-shot overload trips.

Update available via HACS
