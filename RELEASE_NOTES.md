<!-- release: v2.12.1657 -->

## Sungrow optimizer export targets

Sungrow SH systems now apply optimizer export targets using the separate grid-export limit, leaving battery power available to serve the house. This corrects small Cost Neutral export requests when Spread Export is off and preserves the planned contribution from solar.

Failed export commands are also reported as unsuccessful so the optimizer can retry. Small overnight slots can still be part of a valid daily Cost Neutral budget. Update through HACS and restart Home Assistant.
