<!-- release: v2.12.881 -->

## What's Changed

**Stop transient cloud validation failures from opening reauthentication repairs**

PowerSync now confirms that the saved PowerSync bearer is genuinely invalid before asking Home Assistant to reauthenticate. If a cloud authentication check is temporarily unavailable, the cloud returns a retryable service error and Home Assistant retries instead of treating the unchanged token as expired.

This fixes cases where the repair appeared even though Home Assistant and the cloud still held the same working token.

Update available via HACS
