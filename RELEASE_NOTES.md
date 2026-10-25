<!-- release: v2.12.1499 -->
## What's Changed

### Powerwall gateway diagnostics

Powerwall gateway diagnostic sensors now keep their last attempt time and error details visible when unavailable, and retry every five minutes even if live gateway readings keep failing. This improves troubleshooting; charge and discharge still require valid telemetry.
