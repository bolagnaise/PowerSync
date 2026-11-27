<!-- release: v2.12.1865 -->

## Smart Schedule solar charging picks the right hours on Amber

On Amber and Flow Power (Amber pricing), Smart Schedule ranked solar charging hours backwards. It preferred the hours when exporting solar paid the most and showed them as "GET PAID", so a car's solar window could move to the next morning instead of using today's low-value solar. Smart Schedule now treats a higher feed-in price as a higher cost of charging from solar, and **Maximum export price for solar charging** now applies to its planned windows on Amber.

Update through HACS and restart Home Assistant.
