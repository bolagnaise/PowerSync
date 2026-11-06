<!-- release: v2.12.1638 -->

## Amber recovery after a failed tariff upload

Native Max cleanup can now retry a failed normal Amber tariff upload with fresh prices, rather than remain on **Restoring Normal Operation** indefinitely. This also recovers an existing pending session after restart when the site, session and current tariff confirm ownership.

Update through HACS and restart Home Assistant, leaving saved recovery files unchanged. Unavailable prices or readback keep recovery pending for retry; uncertain force commands and another controller's tariff remain protected.
