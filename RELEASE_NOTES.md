## What's Changed

**SAJ force discharge no longer refuses while the inverter is operating normally**
2.12.230 made `inverter_working_mode` the authoritative engagement signal — refuse anything where it isn't `2`. Field history shows that on healthy systems the register oscillates `2 ↔ 4` every 1–3 minutes as the SAJ cycles through its internal states, so force discharge (a one-shot user trigger) was being rejected roughly a third of the time, even with the battery clearly running and the R-phase inverter voltage at ~240 V. Force charge appeared to work because the optimizer retries it constantly and eventually lands on a `working_mode=2` window. The engagement check now refuses only when **both** signals say lockout — `working_mode != 2` AND R-phase voltage `< 50 V`. The original low-SOC lockout (the genuine condition that needed catching: mode 4 + R-phase 0 V together) is still refused with the same clear error and instruction to power-cycle. The earlier firmware bug that motivated 2.12.230 (mode 2 + R-phase 0 V on stanus74) is also still handled correctly, because mode 2 alone is enough to confirm engagement.

Update available via HACS
