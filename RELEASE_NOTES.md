<!-- release: v2.12.1777 -->

## Switch-only charging in Price Level

If your inverter already reduces battery charging as household demand rises, enable **Prioritise switch-only charging in Price Level** in PowerSync's **EV Charging** options. It applies when Price Level allows grid import and phase-aware EV load management is off. PowerSync still checks measured grid import and the EV plan before keeping the charger on or resuming it at full rate.

Update through HACS and restart Home Assistant.
