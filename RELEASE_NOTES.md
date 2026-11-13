<!-- release: v2.12.1693 -->

## Tesla BLE charging already complete

PowerSync now refuses a Start request when fresh vehicle readings say charging is complete. This prevents an unnecessary recovery hold when the BLE bridge declines to restart a car already at its charge limit.

Update through HACS and restart Home Assistant. Existing retained-command holds still require the supported recovery action; this update does not clear them automatically.
