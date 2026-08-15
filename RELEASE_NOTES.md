<!-- release: v2.12.1111 -->

## What's Changed

**Stopped dashboard cards from rebuilding unchanged DOM trees**
The Forecast Summary and Battery Health cards now update their shadow DOM only when their rendered content changes. Frequent Home Assistant state updates no longer tear down and recreate these card trees when their displayed values are unchanged, substantially reducing detached-node and listener growth during long-running PowerSync dashboard sessions without delaying real data updates.

Update available via HACS
