<!-- release: v2.12.1863 -->

## Powerwall 0 W battery hold keeps running

A locally paired Powerwall holding the battery at 0 W could stop partway through when the gateway briefly reported a slightly negative Home Load. The battery then went back to its normal operation. A 0 W hold doesn't depend on home load, so it now continues through these readings. Fixed charge and discharge power still stop on a negative Home Load reading, as before.

## Battery efficiency learning completes more cycles

Battery efficiency learning only checked the battery once per optimization run, about every five minutes. When the battery switched quickly between charging and discharging within that time, learning could discard a cycle that was in progress or measure its energy incorrectly. PowerSync now learns from every battery reading it receives, so cycles complete more reliably and their efficiency estimates are more accurate. The planning value still changes only after enough valid cycles over several days.

Update through HACS and restart Home Assistant.
