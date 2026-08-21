<!-- release: v2.12.1176 -->

## What's Changed

**Safer GoodWe DC Solar Curtailment hand-off to force discharge**
GoodWe force-discharge commands now fail closed when PowerSync cannot verify
that a prior zero-export curtailment limit was released. This applies to both
optimizer-driven and manual force-discharge paths, preventing an export command
from being issued while the inverter still reports the containment limit.

**Verified GoodWe force-discharge refreshes**
A failed GoodWe force-discharge hardware refresh is now surfaced to the
optimizer instead of being logged as a successful extension.

Update available via HACS
