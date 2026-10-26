<!-- release: v2.12.1515 -->
## Powerwall control method recovery

Changing the preferred battery control method now attempts any unfinished Tesla settings restore after stopping local control. Previously, the change could save Tariff while an older restore still blocked it from starting.

If restoration cannot be confirmed, PowerSync keeps the previous control preference and reports that cleanup remains pending. This does not bypass Tesla grid-charging confirmation checks. Update through HACS, restart Home Assistant, then retry the control-method change.
