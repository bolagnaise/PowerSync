<!-- release: v2.12.1781 -->

## Cost Neutral charging with two EVs

PowerSync now keeps valid charging plans when the solver leaves tiny rounding differences in the planned EV power. This avoids unnecessary fallback planning while preserving the checks that each car's planned draw has an eligible source.

Update through HACS and restart Home Assistant.
