<!-- release: v2.12.1696 -->

## Zaptec price limits after authentication

Price-Level Charging now stops externally started standalone Zaptec charging when the price is above your configured limit and PowerSync controls the session. Previously, authenticating at the charger could leave it charging despite a correct decision to wait.

Hands-off external charging and sessions owned by another charging mode remain protected. Update through HACS and restart Home Assistant.
