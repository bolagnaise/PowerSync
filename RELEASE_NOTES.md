## What's Changed

**Fix Integration Load Crash (GloBird and Others)**
v2.12.102 introduced a startup crash for users with certain integrations installed alongside PowerSync. The orphaned sub-device cleanup code assumed all device identifiers were 2-tuples, but some integrations use 3-element tuples — causing `ValueError: too many values to unpack` and preventing PowerSync from loading. Fixed to handle any identifier tuple length.

Update available via HACS
