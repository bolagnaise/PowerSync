<!-- release: v2.12.1468 -->
## Local Powerwall battery control

Paired Powerwalls with a local gateway address now use direct power commands for Force Charge, Force Discharge and Smart Optimisation. PowerSync can request a charging rate or an export target without changing your tariff or permanent backup reserve. Existing Tesla controls remain the fallback when local control fails and has been safely released. No extra switch is required.

Commands expire automatically if Home Assistant stops, and restart recovery releases an old local command instead of replaying it. Monitoring Mode, reserve, charge limits, demand protection and export guards still apply. Local charging, discharging and resume were tested on a Powerwall 3; support depends on gateway firmware.

Thanks to **Scotty (`cool.scotty`)** and **Brett (Teslemetry)** for helping bring this feature to PowerSync.
