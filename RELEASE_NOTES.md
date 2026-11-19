<!-- release: v2.12.1755 -->

## Powerwall local renewals and Force Power

If a fixed-output renewal's battery reading becomes stale before PowerSync can send the request, PowerSync now tries one fresh read within the original dispatch window. The same freshness, safety and deadline checks still apply.

The Force Power slider for paired Powerwalls now keeps a stable range in Home Assistant. Live charge and export limits still apply when you use it.

Update through HACS and restart Home Assistant.
