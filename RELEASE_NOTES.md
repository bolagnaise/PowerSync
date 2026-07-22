<!-- release: v2.12.912 -->

## What's Changed

**Powerwall gateway URLs now work for local control**
PowerSync now normalizes pasted `http://` or `https://` Powerwall gateway addresses before building the fixed local HTTPS endpoint. Existing saved URLs are migrated automatically, so local Force Charge and other Powerwall commands no longer fall back to Tesla cloud because of malformed addresses such as `https://http://...`. Invalid schemes, credentials, paths, queries, and ports are rejected instead of silently contacting a different endpoint.

Update available via HACS
