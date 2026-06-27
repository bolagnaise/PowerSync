<!-- release: v2.12.727 -->

## What's Changed

**Read FoxESS KH daily battery totals from holding registers when needed**
PowerSync now retries the FoxESS direct Modbus daily battery charge and discharge counters as holding registers when the inverter rejects the input-register read. This fixes KH/H3-style setups where live power readings work but the FoxESS daily battery totals fall back to the software accumulator because registers `31088` and `31089` return a Modbus illegal-address response on the input-register path.

Update available via HACS
