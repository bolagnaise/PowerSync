<!-- release: v2.12.574 -->

## What's Changed

**Stabilize GoodWe Modbus TCP polling**
PowerSync now serializes its own GoodWe Modbus TCP register reads and writes on a single client connection. This prevents overlapping PowerSync requests from confusing pymodbus transaction IDs, reducing repeated `request ask for transaction_id...` and `Cancel send, because not connected` errors in GoodWe direct TCP setups.

**Add GoodWe request-concurrency regression coverage**
The GoodWe Modbus controller now has focused tests for concurrent read and mixed read/write calls so future changes keep the TCP request path serialized.

Update available via HACS
