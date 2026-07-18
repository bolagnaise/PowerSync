<!-- release: v2.12.885 -->

## What's Changed

**Stop unsupported Tesla Hermes retries cleanly**

When Tesla explicitly reports that a third-party access token cannot use the
private Hermes signaling channel and must use `signed_command`, PowerSync now
marks that signaling route unavailable immediately. It no longer retries
alternate Hermes endpoints or falls back to presenting the raw access token to
the WebSocket, avoiding repeated requests that cannot succeed.

Normal Tesla Fleet signed commands, local Tesla BLE control, and unrelated
temporary precondition failures are unchanged.

Update available via HACS
