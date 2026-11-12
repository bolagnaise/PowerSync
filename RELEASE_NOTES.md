<!-- release: v2.12.1680 -->

## Recover EV headroom after charging ends

A finished Tesla charging session could leave its EV headroom reservation blocking Powerwall grid charging, even after earlier command recovery succeeded. Read-only recovery now accepts fresh stopped-vehicle proof from the same verified provider after the session ends, while preserving ownership, identity and unresolved-command safety checks.

The recovery inspection now shows the headroom reservation, route checks and native-source selection result, so a refused attempt identifies the remaining blocker. Update through HACS and restart Home Assistant. Existing reservations can recover from qualifying fresh reports or the **Recover EV charging headroom** action with provider-refresh consent; this does not wake the car or issue charging commands.
