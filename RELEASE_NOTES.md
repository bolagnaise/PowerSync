<!-- release: v2.12.1477 -->
## Powerwall charging limits

Paired Powerwalls now learn a lower charging limit reported by the gateway and retry once at that limit after the gateway acknowledges a request to resume normal operation. Manual controls and subsequent requests use the reported ceiling without changing your configured battery rating. A failed local command cannot use tariff control to bypass a learned limit.

## Spread Import with EV charging

Spread Import now keeps room for PowerSync-managed EV charging when spreading battery charging across a window. Native Tesla charge windows remain included as forecast demand, and your configured site import limit continues to apply.
