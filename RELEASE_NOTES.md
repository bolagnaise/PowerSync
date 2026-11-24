<!-- release: v2.12.1822 -->

## Correct average cost display in Today so far

The dashboard's Today so far card now shows the average net energy cost in the right unit at any value. Previously an average of 5 dollars per kWh or more was shown 100 times too small, for example 5.4 c/kWh instead of 538 c/kWh, which could hide a wrongly entered supply charge.

## Clearer daily supply charge setting

The daily supply charge description in the optimization settings now says to enter the amount per day in your currency rather than cents, for example 1.67 for 166.67 c/day. If your Net cost today looks far too high, check this setting. Update through HACS and restart Home Assistant.
