<!-- release: v2.12.766 -->

## What's Changed

**Restore stale battery reserves after optimizer reloads**
PowerSync now checks for reserve-based battery systems that were left at an optimizer IDLE hold after Smart Optimization was disabled or Home Assistant restarted. When the live reserve is clearly stuck at the current SOC, no force or hold mode is active, and the house is importing while the battery is idle, PowerSync restores the configured reserve and normal work mode instead of leaving the battery blocked.

**Broader protection for reserve-based batteries**
The startup cleanup applies to supported non-Tesla reserve-based systems including Sungrow, FoxESS, Solax, Fronius Reserva, Neovolt, SolarEdge, and Anker Solix, while skipping Tesla, Sigenergy, GoodWe, and custom controllers where the same stale-reserve pattern does not apply.

Update available via HACS
