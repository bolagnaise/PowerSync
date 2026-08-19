<!-- release: v2.12.1148 -->

## Per-phase EV load management now has a settings screen — and Boost, manual, and quick charging can no longer bust the budget

PowerSync's per-phase EV current limiting was previously reachable only from the mobile app. It now has a full Home Assistant settings screen, and three charging paths that used to walk straight past the shared phase budget have been brought under it.

**Nothing changes unless you turn phase load management on. It stays off by default.**

### New: PowerSync → Configure → EV load management

Everything the mobile app's Home Power screen configures is now editable from Home Assistant:

- **Home electrical phases** — single or three phase.
- **Enable phase-aware EV load management** — the master switch.
- **Mains limit per phase** — the continuous current limit for each phase. Use the site-approved value.
- **Safety margin per phase** — headroom held back below that limit to absorb sensor and charger command latency.
- **L1 / L2 / L3 grid current sensors** — Home Assistant sensors measured at the grid connection, reporting amps. A single-phase supply only needs L1.

Both the settings screen and the mobile app write the same stored settings and are checked by the same validator, so they cannot drift apart or disagree about what is valid. If a combination is rejected, the screen names the specific reason rather than a generic failure.

### Boost, manual, and quick charging now respect the shared budget

With phase management enabled, these three paths previously set a charge current and were never revisited:

- **Boost** sent a raw start followed by a raw "set 32 A" command. It never ramped, and never consulted the phase budget at all — a boost could load a phase past its limit on top of whatever else was already drawing.
- **Manual charging** was recorded with no periodic controller. Whatever current the charger happened to be left on was held indefinitely, occupying headroom the allocator had no way to reclaim for another car.
- **Quick charge** armed its stop deadline in the same slot the periodic controller uses, so on any session that *did* have a controller, setting a quick-charge duration silently cancelled phase enforcement for the rest of the window.

All three now run as managed fixed-rate sessions. The current you asked for is unchanged — Boost still targets 32 A, manual still charges at the rate you chose — but it is re-applied through the shared allocator on every control cycle. When the site gets busy the rate comes down; when headroom returns it goes back up. A Boost or manual start with no headroom at all is now refused up front instead of energising the charger and clamping it a moment later.

Boost also arbitrates properly now: it takes over from Smart Schedule, Scheduled Charging, Price-Level, and Solar Surplus sessions, while still yielding to a later manual command. Its expiry stop tears the controller down and releases ownership in one step.

### Better visibility

The normalized loadpoint endpoint now reports a `load_management` block per loadpoint alongside the existing site-level summary — measured phase currents, the limiting phase, remaining per-phase budgets, what this loadpoint was allocated, and why. If a Boost lands during a busy period, the response says how many amps it actually got.

### Still not a substitute for breaker protection

This is responsive software control over Home Assistant telemetry. Sensor update and charger command latency still apply, and readings older than 90 seconds are treated as unsafe and drop managed EV charging to 0 A. Keep your site-approved over-current protection in place and pick a safety margin appropriate to how quickly your sensors and charger actually respond.
