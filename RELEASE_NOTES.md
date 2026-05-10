<!-- release: v2.12.375 -->

## What's Changed

**Non-Tesla batteries no longer get their hardware reserve overwritten in self-consumption**
PowerSync now limits the self-consumption backup-reserve rewrite to Tesla systems, where that behavior is needed to avoid unwanted grid charging. GoodWe and other Modbus-style batteries keep their real hardware reserve or DOD setting during normal self-consumption, so an optimizer floor such as 45% will no longer be written back to the inverter as a hardware reserve after startup.

Update available via HACS
