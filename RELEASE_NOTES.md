<!-- release: v2.12.1279 -->

## What's Changed

**Tesla Solar Surplus starts at its calculated rate**
Fixed a Tesla BLE Solar Surplus start path that could omit the calculated initial charging current. When a stopped Tesla needs its first rate during BLE start confirmation, PowerSync now supplies the already-approved Solar Surplus target so a confirmed start can settle without an unnecessary compensating stop. Eligibility, external-ownership protection, and unconfirmed-command safeguards are unchanged.

Update available via HACS
