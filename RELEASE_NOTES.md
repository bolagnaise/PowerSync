<!-- release: v2.12.1484 -->
## Full-rate control for larger Powerwall systems

Powerwall sites reporting more than 10 kW now use Tesla tariff control for sustained Max charging, Max discharge and optimiser actions, with a short local startup command when safe. Numeric power targets retain bounded local control; reserves, import/export limits and EV ownership still apply.

## Safer control handovers

Stop, direction changes and restart recovery retain one restoration owner and the original finish time, without repeating the startup command each optimiser cycle. A brief transition in power is possible; Battery Mode distinguishes pending or acknowledged requests from measured battery power.
