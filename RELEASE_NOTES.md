<!-- release: v2.12.1096 -->

## What's Changed

**Retry unconfirmed Tesla reserve writes safely**

Fixed a Tesla Powerwall reserve-control reliability gap where a failed or blocked backup-reserve write could be treated as successful by Smart Optimization. PowerSync now requires a confirmed service result before recording optimizer ownership, keeps failed writes retryable, and does not persist an unconfirmed manual reserve setting.

Update available via HACS
