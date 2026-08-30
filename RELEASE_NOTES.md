<!-- release: v2.12.1211 -->

## What's Changed

**Prevent scheduled updates from corrupting HACS repository state**
PowerSync now refreshes and installs scheduled updates only through Home Assistant's supported update-entity services. It no longer force-calls HACS repository internals, which could turn a temporarily unavailable repository tree into the invalid path `custom_components/None` and make both automatic and manual HACS downloads fail. Existing retry and pending-restart handling remain in place.

Update available via HACS
