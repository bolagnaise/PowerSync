<!-- release: v2.12.916 -->

## Fixed

- **Sigenergy No Idle holds can now absorb solar**: When Charge By Time must retain a protective idle hold while No Idle is enabled, PowerSync now blocks discharge without putting Sigenergy batteries into standby. Surplus solar can continue charging the battery, and the temporary discharge cap is restored with retry protection when the hold ends.
