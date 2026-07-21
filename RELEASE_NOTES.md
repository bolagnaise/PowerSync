<!-- release: v2.12.907 -->

## What's Changed

### AlphaESS cloud-only monitoring

AlphaESS systems can now use the official cloud API without a local Modbus connection. Cloud-only setups are explicitly monitoring-only, automatically select the sole system on an account, and request a serial number when multiple systems are available. Existing AlphaESS Modbus setups keep their current behavior.

### Standalone GoodWe MS inverter control

PowerSync can now discover and monitor a standalone GoodWe MS AC inverter through the GoodWe Experimental Home Assistant entities. AC curtailment uses the inverter's export-limit controls transactionally, restoring the exact previous state after curtailment, rollback, or restart.

### Amber metered cost for today

Amber users now receive a fresh partial-day metered cost sensor and API response for today, including import cost, export earnings, and net cost. The dashboard keeps this metered result separate from the optimizer estimate and suppresses stale or incomplete data instead of showing a misleading zero.

Update available via HACS
