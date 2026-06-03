<!-- release: v2.12.551 -->

## What's Changed

**Startup and reload load is reduced for smaller Home Assistant hardware**
PowerSync now defers the first optimizer solve for 90 seconds after startup and holds back dynamic price-triggered re-optimizations during that same window. AEMO dispatch-triggered TOU syncs are also debounced during startup, so Amber/AEMO refreshes, tariff sync, and the LP optimizer no longer pile onto the same Home Assistant reload window.

**Tesla self-consumption recovers stale 100% reserve after force-charge state**
When a Tesla battery is left at a 100% backup reserve from stale force-charge state, the optimizer now treats that as stale control state and reapplies the intended self-consumption reserve. This prevents the battery from staying pinned at full reserve when the schedule has moved back to normal home-battery operation.

**GoodWe export curtailment preserves the user's export settings**
GoodWe zero-export curtailment now enables the inverter's grid export limiter before setting the limit to 0W, then restores the previous limiter state and export limit when curtailment is released. This avoids leaving GoodWe systems stuck with a generic maximum export value after a curtailment session.

**GoodWe curtailment is periodically re-applied while export remains uneconomic**
When GoodWe curtailment stays active, PowerSync now reapplies the export-limit command periodically instead of assuming the first write remains in force indefinitely. This improves reliability for GoodWe systems that may drop or overwrite the export limit while negative or low export pricing continues.

**Sigenergy EV status now shows idle EVAC and EVDC chargers**
The EV status sensor now reads Sigenergy charger state directly when coordinator data is not available and labels EVAC and EVDC chargers correctly. This keeps plugged-in Sigenergy vehicles visible on the dashboard even when they are idle and not drawing charging power.

Update available via HACS
