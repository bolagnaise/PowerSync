<!-- release: v2.12.1550 -->

### Powerwall charging permission checks

Manual Max and scheduled native charging now refuse missing or invalid grid-charging permission instead of treating an omitted field as permission to charge. Local V1R and Automatic plans also keep grid refills unavailable while gateway readings are missing or stale; refills become eligible again when a current readable setting returns.

This update does not enable Local V1R charging when the gateway still omits its permission setting. Tariff control retains its existing confirmation checks. Update through HACS and restart Home Assistant.
