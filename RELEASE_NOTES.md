<!-- release: v2.12.1534 -->

## What's Changed

**Powerwall export confirmation**
PowerSync now recognises Tesla's energy-style update acknowledgement when changing or restoring Grid Export, avoiding a false failure before the setting can be checked. The exact export setting must still be readable and match the request; missing or conflicting confirmation continues to block discharge and keep restoration pending.

Update through HACS and restart Home Assistant.

Update available via HACS
