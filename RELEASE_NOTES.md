<!-- release: v2.12.743 -->

## What's Changed

**Improved VIN redaction in EV charging logs**
PowerSync now masks Tesla VINs when they appear as standalone values in debug logs, including vehicle status messages, parenthesized identifiers, and dictionary-style status output. Existing `VIN: ...` redaction remains in place, and the broader masking closes gaps where EV scheduling and price-level charging diagnostics could expose a raw VIN while troubleshooting.

Update available via HACS
