<!-- release: v2.12.1686 -->

## Battery charging cost tracking

PowerSync now keeps measured solar and grid charging correctly attributed when charging pauses before the battery's charge level catches up. This prevents that measured energy from being labelled unknown and unnecessarily increasing the cost used to decide whether to export.

Update through HACS and restart Home Assistant. Existing unknown-origin energy is preserved; this update does not reset charging history or bypass export safeguards.
