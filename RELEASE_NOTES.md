<!-- release: v2.12.1344 -->

## What's Changed

**Use Home Assistant's core dependencies**

Removed redundant `aiohttp` and `cryptography` requirements from the integration
manifest to comply with Home Assistant's current custom-integration validation.
Both libraries are already supplied by Home Assistant, including the minimum
supported version, so PowerSync continues to use them without declaring
separate installation requirements.

This release retains the Smart Schedule and deadline charging site-import
limit fixes published in v2.12.1343.

Update available via HACS.
