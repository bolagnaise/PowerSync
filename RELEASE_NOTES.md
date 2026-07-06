<!-- release: v2.12.771 -->

## What's Changed

**FoxESS force charge/discharge now keeps remote-control ownership**
FoxESS solar curtailment no longer re-applies its zero-export remote-control command while a force charge or force discharge session is active. This fixes H3 Smart/direct Modbus cases where a free-import force charge would start successfully, then drop back to self-use when the curtailment refresh wrote a 0 W remote limit during the same window.

Update available via HACS
