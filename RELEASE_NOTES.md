<!-- release: v2.12.1817 -->

## Tesla Boost with Solar Surplus

When PowerSync uses both Tesla Fleet and Bluetooth, Boost can take over from Solar Surplus if the vehicle is already stopped. Fresh readings from the matched Bluetooth bridge can confirm the car is idle even when Tesla Fleet has no recent reading. Solar Surplus also avoids sending an unnecessary Stop to an idle car when checking its charge ceiling.

Update through HACS and restart Home Assistant.
