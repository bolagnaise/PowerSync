<!-- release: v2.12.1714 -->

## Clearer Tesla charging status

Tesla charging retry messages now say when PowerSync could not establish charging control. Previously, they could incorrectly suggest that physical charging had not been observed when an ownership or stop-confirmation check prevented PowerSync from taking control.

Charging safety checks and retry timing are unchanged. Update through HACS and restart Home Assistant.
