<!-- release: v2.12.1668 -->

## Faster Home Assistant startup

Fixed a PowerSync support recorder task that could hold up Home Assistant startup until it timed out. Support recording continues in the background and still stops when the integration unloads.
