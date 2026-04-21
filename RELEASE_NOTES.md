## What's Changed

**Sigenergy Modbus Register Spam Fixed**
Every 15-second Modbus poll was logging `'float' object has no attribute 'to_bytes'` for registers 30068 and 30070 (plant slave 247). The slave ID and port were being stored as floats (247.0, 502.0) in config entry data and passed directly to pymodbus, which expects integers. The base inverter controller now always casts `port` and `slave_id` to `int` on construction, fixing the spam for all Modbus-based inverters (Sigenergy, Sungrow, FoxESS, GoodWe, AlphaESS).

Update available via HACS
