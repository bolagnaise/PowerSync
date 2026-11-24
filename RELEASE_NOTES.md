<!-- release: v2.12.1813 -->

## Tesla Bluetooth and Boost recovery

PowerSync can now recover supported Tesla Bluetooth bridges in the background when fresh readings show the matched car is stopped at zero current and power. Boost checks for an existing Bluetooth hold before it records a new session or changes the charge limit. When PowerSync says recovery is finished, you can tap Boost again straight away.

## Keep usable optimiser plans

The optimiser keeps a valid plan when an optional Cost Neutral budget update or backup-reserve adjustment fails. It continues using the settings and limits that made the earlier plan safe to run.

Update through HACS and restart Home Assistant.
