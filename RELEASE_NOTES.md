<!-- release: v2.12.1718 -->

## Plan for EV demand before the car gets home

The new **Use historical EV data** setting under **Battery & forecast inputs** learns weekday return times and remaining driving consumption. While your car is away, Smart Optimization can prepare the home battery during cheaper hours for the EV demand expected after it returns, or wait when charging later is more economical.

This setting is off by default. Learning requires at least two comparable weekday trips with usable vehicle battery and home-presence observations. Actual vehicle data replaces the prediction on return. Your existing charging targets, deadlines, price limits, source permissions and home-battery preservation rules still apply; a predicted visit never authorises an EV command.

Update through HACS and restart Home Assistant, then enable the setting if wanted. Missing history keeps the existing planning behaviour while PowerSync learns.

## Save battery energy for expensive household use

Smart Optimization now preserves a planned battery hold for later household use at a higher import price, including peaks more than six hours away. Existing economic hold reasons remain intact. This applies when Idle is allowed; Disable Idle continues to permit natural self-consumption.
