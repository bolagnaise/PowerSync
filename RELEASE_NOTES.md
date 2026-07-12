<!-- release: v2.12.829 -->

## What's Changed

**Fix ZeroHero pre-export top-ups**
PowerSync no longer inflates GloBird ZeroHero's capped Super Export bonus with the current import-price spread. This prevents Smart Optimization from buying expensive grid energy to top up an already-full battery before a 15c/kWh Super Export window when the economics do not justify it.

**Keep Sigenergy Remote EMS in monitoring mode**
Enabling monitoring mode now cleans up active PowerSync force commands without handing Sigenergy back to native/VPP control when Smart Optimization still owns dispatch. Sigenergy systems stay in Remote EMS/self-consumption so Modbus control remains ready after monitoring is enabled.

Update available via HACS
