<!-- release: v2.12.1725 -->

## Recover Support after a busy Home Assistant startup

The Support card could keep returning `404: Not Found` when its initialization timed out during startup. PowerSync now makes one automatic retry after Home Assistant finishes starting, allowing Support to recover when the temporary delay clears.

This does not restore a temporary support session or approve new access. Update through HACS and restart Home Assistant. Other startup errors may still require investigation.
