<!-- release: v2.12.355 -->

## What's Changed

**Fix free import optimizer detection after Home Assistant tariff refresh**
PowerSync now reads Tesla TOU tariff periods with the same parser used by the live tariff sensors, including nested `periods` entries returned after recent Home Assistant/Tesla tariff refreshes. This keeps monitoring mode and the optimizer schedule showing force charge during free import windows instead of falling back to shoulder pricing.

**Make TOU matching rate-based instead of name-based**
The optimizer and tariff price paths no longer rely on labels such as PEAK, OFF_PEAK, or SUPER_OFF_PEAK when overlapping Tesla periods are used to represent plans with more than four windows. Matching now resolves by the active time window and tariff rates, so creatively named periods still use the right buy and sell prices.

Update available via HACS
