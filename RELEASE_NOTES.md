<!-- release: v2.12.342 -->

## What's Changed

**Force controls exposed as Home Assistant switches**
Force Charge and Force Discharge are now exposed as Home Assistant switch entities for every supported battery system, not just Tesla Powerwall. Sigenergy, FoxESS, GoodWe, Sungrow, AlphaESS, ESY Sunhome, Solax, SAJ H2, and Neovolt users can now trigger and stop manual force modes from standard HA automations while PowerSync continues to use the existing service-based control path underneath.

**Force duration and power controls now work together**
The new switches use the selected Force Charge and Force Discharge duration controls, and they pass the configured Force Power value through to batteries that support power-limited force commands. The force switch state also follows dashboard and service-triggered force modes, so the HA entity stays aligned even when the action starts from the PowerSync dashboard.

Update available via HACS
