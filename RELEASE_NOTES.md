<!-- release: v2.12.420 -->

## What's Changed

**Fix Sungrow setup validation on partial Modbus implementations**
PowerSync now validates Sungrow setup with the core battery register block instead of requiring every optional load, export, and control register to respond during onboarding. This lets SH/WiNet systems complete setup when SOC and battery health are readable but some optional registers time out or close the Modbus session.

Update available via HACS
