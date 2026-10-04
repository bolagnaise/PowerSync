<!-- release: v2.12.1348 -->

## What's Changed

**Redact Tesla site IDs in INFO logs**

Tesla site IDs are now obfuscated in Tesla startup, provider-selection, and
capability-probe INFO messages, including short IDs and deferred-format
records. The original identifier remains unchanged for API requests and
capability handling.

This is a logging privacy correction only; it does not change optimizer,
hardware-control, or Monitoring Mode behavior.

Update available via HACS
