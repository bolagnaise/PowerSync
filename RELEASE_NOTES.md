<!-- release: v2.12.1536 -->

## What's Changed

**Powerwall Tariff export requests**
Tariff discharge now avoids another Grid Export write when a fresh gateway or cloud reading already confirms battery export is enabled. Successful Tesla update acknowledgements are also recognised across the supported HTTP success statuses. Any remaining export-request failure now shows its HTTP status and parsed reason when available.

Battery export remains enabled after Tariff discharge, and the existing recovery and control protections remain in place. Update through HACS and restart Home Assistant.

Update available via HACS
