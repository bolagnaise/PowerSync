<!-- release: v2.12.661 -->

## What's Changed

**Sungrow low-power force charge fix**
Sungrow SH forced charge and discharge commands now clamp very small optimizer setpoints to the inverter's practical 200 W minimum. This prevents low planned values, such as an 89 W top-up, from being accepted by PowerSync but producing no actual battery movement.

**Dashboard tooltip transparency**
Dashboard graph tooltips now use the restored translucent blurred background while remaining layered above the graph lines. This fixes the solid tooltip appearance that could still show on the latest release.

Update available via HACS
