<!-- release: v2.12.1723 -->

## Support startup diagnostics

If PowerSync's support component cannot start, its Home Assistant warning now includes the error type and code locations to help support identify the failure. Exception messages and local values are excluded, and the core integration continues running.

This improves diagnosis of a Support card that cannot list installations; it does not automatically repair every cause. Update through HACS and restart Home Assistant when convenient.
