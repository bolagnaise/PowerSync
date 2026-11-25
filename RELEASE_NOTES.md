<!-- release: v2.12.1829 -->

## Powerwall charge sessions finish normally at the charge limit

When a local Powerwall charge session reaches the configured battery charge limit, PowerSync returns control to Tesla and records that the target was reached. Previously, reaching the limit during a session could produce a safety error even though charging stopped as intended.

## Cost Neutral planning finishes faster

The optimiser uses fewer steps for later days in the forecast and spends less time refining plans that are already within half a cent of the best result. This helps avoid timeouts on slower Home Assistant hardware.

## Tesla controls share a gateway status refresh

When several Tesla controls refresh at once, they share one site settings request. This reduces repeated calls while keeping the controls up to date after a setting changes.

Update through HACS and restart Home Assistant.
