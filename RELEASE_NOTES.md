<!-- release: v2.12.1762 -->

## Powerwall Home Load diagnostics

For locally paired Powerwalls, the Home Load sensor now records the raw meter value from the same source as the chart. If a negative gateway Home Load reading stops a fixed-power renewal, the log includes the reading in watts.

## Tesla charging current increases

Solar Surplus now tracks the charge current it sent after Home Assistant applied the Tesla current limit. This lets PowerSync retry a higher rate through Tessie when the limit rises and there is still enough surplus.

Update through HACS and restart Home Assistant.
