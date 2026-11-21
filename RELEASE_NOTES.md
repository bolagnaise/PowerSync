<!-- release: v2.12.1773 -->

## Smart Schedule charging during an active window

When Smart Schedule refreshes an active EV charging window partway through an optimizer interval, PowerSync now keeps that interval eligible. This prevents an unnecessary pause during a free charging window while preserving configured site limits.

Update through HACS and restart Home Assistant.
