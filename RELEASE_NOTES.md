<!-- release: v2.12.1509 -->

### Sungrow iHomeManager discharge reserve

Native iHomeManager battery control now uses the same configured backup reserve as the optimiser. Older saved settings can no longer apply a lower discharge floor, and active discharge sessions stop renewing when the current reserve is reached.

Update through HACS and restart Home Assistant. This corrects the software discharge guard; it does not change the inverter's persistent reserve setting.
