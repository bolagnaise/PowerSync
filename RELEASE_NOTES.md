<!-- release: v2.12.1107 -->

## What's Changed

**Charged EVs throughout free electricity periods**
Smart Schedule now starts an eligible plugged-in vehicle during active 0 c/kWh periods across every strategy, including Solar Only and Meet Deadline when the free window covers only part of the energy needed. Remaining energy stays planned before departure, while target, location, demand-window, charger, and grid constraints remain enforced.

**Followed real battery acceptance while protecting the site limit**
Free-period EV current now learns the home battery's live acceptance from consistent samples with proven site-import headroom, including batteries that taper below 90% SOC. The session-local model keeps a safety margin, immediately reserves recovered battery demand, reduces EV current if household load returns, and still treats the active charger current limit as a hard ceiling.

Update available via HACS
