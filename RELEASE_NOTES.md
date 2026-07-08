<!-- release: v2.12.796 -->

## What's Changed

**AEMO spike checks tolerate transient price API timeouts**
PowerSync now treats AEMO JSON fallback timeouts the same as other transient AEMO fetch failures. If NEMWEB and the fallback API are both slow during an automatic spike check, PowerSync skips that check cleanly instead of leaking a Home Assistant "Task exception was never retrieved" traceback.

Update available via HACS
