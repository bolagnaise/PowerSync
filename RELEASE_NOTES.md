<!-- release: v2.12.1479 -->
## Stop repeated charging attempts at full SOC

Paired Powerwall systems now use fresh gateway SOC to stop the optimiser retrying a cached charge when the battery has reached its charge ceiling, including 100%. Normal operation is restored once, and charging can resume after SOC falls below the ceiling.

## Clearer charge logs

Optimiser logs now label requested charge power explicitly, so a requested rate is not mistaken for the lower target accepted by the gateway. This update does not remove a gateway's timed-command power ceiling.
