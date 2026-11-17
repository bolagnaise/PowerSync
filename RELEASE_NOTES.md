<!-- release: v2.12.1729 -->

## Supervised Powerwall settings recovery

Powerwall sites blocked by an old unresolved local settings request now have an administrator recovery action. It inspects the current configuration, retains the original incident and restore evidence, and can resume local settings and persistent site limits without sending a device command.

Recovery requires explicit acceptance that the earlier full configuration may still apply and overwrite newer settings. New uncertain writes block controls again. Update through HACS, restart Home Assistant, then use **Developer tools → Actions → PowerSync: Recover Powerwall local settings under supervision**, starting with **inspect**.
