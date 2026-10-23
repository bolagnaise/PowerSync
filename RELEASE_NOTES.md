<!-- release: v2.12.1471 -->
## Powerwall site power limits

Paired local Powerwall controls now read the gateway’s configured import and export limits automatically. The Force Power slider follows those limits as they change, using the learned battery discharge limit when no export limit is set. Each command still respects the battery’s hardware limit and current home and solar power.

## Safer handling when local data is unavailable

A saved kW target stays explicit when local telemetry is lost. PowerSync blocks tariff fallback when a site limit is configured or cannot be confirmed, so an unavailable local command cannot silently become full-rate charging or discharging.
