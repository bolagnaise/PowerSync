## What's Changed

**Force Charge/Discharge: Power Slider**
Manual force charge and force discharge now expose a power level control. On the app, the fixed 1 kW / 2 kW / 5 kW / 10 kW / Max buttons are replaced with a continuous slider from 0.5 kW to the BMS-reported maximum — dragging to the right end sets Max (uses the inverter's rated power). On the HA dashboard, a `Force Power` slider card appears above the Force Charge/Discharge buttons for compatible battery systems (FoxESS, GoodWe, Sigenergy, Sungrow, AlphaESS); set it to 0 for automatic max. The selected power level is shown in the confirmation prompt.

Update available via HACS
