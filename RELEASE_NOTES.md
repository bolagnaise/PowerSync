<!-- release: v2.12.1476 -->
## Powerwall charge and discharge diagnostics

When a gateway returns an error for a local battery command, PowerSync now records its error code and a short explanation in the log. This helps identify permission, power-limit and firmware-support problems that previously appeared only as a missing acknowledgement.

This update improves diagnostics for the reported Powerwall 2 failures; it does not yet establish their cause or change command acceptance, restoration or tariff fallback.
