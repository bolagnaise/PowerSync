<!-- release: v2.12.647 -->

## What's Changed

**Avoid false empty-battery plans for Fronius GEN24 storage**
PowerSync no longer treats an unavailable Fronius GEN24/BYD storage SOC sensor as `0%` while Home Assistant is restarting or the upstream Fronius Modbus entities are briefly reconnecting. If a previous valid reading is available, PowerSync keeps using it; otherwise the optimizer treats the SOC as unknown instead of planning from an empty battery.

This prevents charge windows from showing misleading starts such as `2%` when the real battery SOC is already much higher.

Update available via HACS
