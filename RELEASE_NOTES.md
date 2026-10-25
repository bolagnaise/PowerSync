<!-- release: v2.12.1498 -->

## What's Changed

**Start Powerwall tariff charging reliably during price updates**
Native Max charging could fail to start when a PowerSync price update was still being uploaded. PowerSync now lets that update finish before saving the tariff to restore, then keeps new price updates out of the charging handoff. Changes made by another controller remain protected.

Update through HACS and restart Home Assistant. This fixes the overlapping-upload failure; it does not establish the cause of every charging-rate change or pending physical settling report.

Update available via HACS
