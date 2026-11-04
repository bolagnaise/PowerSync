<!-- release: v2.12.1622 -->

## Powerwall pairing during recovery

PowerSync now keeps the existing Gateway pairing and address while Native Max cleanup is pending, including after a restart. This prevents re-pairing or connection changes from stranding the saved settings. A pairing that cannot save its settings is reported as failed.

Update through HACS and restart Home Assistant. Finish Restore Normal before changing pairing settings. This update does not clear a recovery record already tied to an older pairing; retain that record and contact support if the identity warning is already present.
