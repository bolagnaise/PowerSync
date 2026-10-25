<!-- release: v2.12.1501 -->

### Powerwall charging at full SOC

The optimiser now checks fresh local battery SOC before requesting paired Tariff charging, including Automatic when it selects tariff control. A full battery stops repeated Max-charge attempts, and charging can resume when SOC falls below the configured ceiling.

### Powerwall tariff refusal details

When native tariff charging is refused, the error now includes the specific failed admission check, such as the grid-import limit or battery SOC. Existing site limits and control ownership still apply; this does not repair gateways that return no usable local telemetry.

Update through HACS and restart Home Assistant.
