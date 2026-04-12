# PowerSync Improvements Roadmap

## Phase 1 — Trust & Transparency (Weeks 1-4) `COMPLETED`

**Goal**: Build user trust by proving PowerSync's value and making every decision transparent.

### What was built

| Feature | Problem it solves |
|---------|------------------|
| **Savings History Store** | Daily savings reset at midnight with no history. Users couldn't see week/month/lifetime trends. Now stores 90-day rolling history + separate cumulative all-time totals that survive the 90-day rotation. |
| **8 New Sensors** | No way to surface savings data in dashboards or automations. Added: `savings_today`, `savings_this_week`, `savings_this_month`, `savings_lifetime`, `daily_cost_total`, `daily_baseline`, `roi_percentage`, `last_decision`. |
| **Decision Log** | Optimizer made decisions silently — users had no idea why the battery was charging at 2am. Now every action gets a human-readable reason: "Charging battery at 8c/kWh — storing for 35c/kWh peak export at 17:00". 288-entry ring buffer (24h), persisted across restarts. |
| **Decision Log API** | No programmatic access to optimizer reasoning. `GET /api/power_sync/decision_log` returns the full history as JSON for the mobile app and frontend. |
| **Savings Notifications** | Users had to check dashboards to know if PowerSync was helping. Now pushes daily summary at 21:00 ("Saved $4.20 today"), weekly on Sundays, and real-time alerts on significant action changes ($0.50+ impact). Dual delivery: Expo Push (iOS/Android app) + optional HA notify. |
| **Savings Card** | No dedicated savings visualization. New `power-sync-savings` Lovelace card with hero savings number, period tabs (Today/Week/Month/All Time), cost breakdown bar, energy stats, decision feed, and ROI progress bar. |
| **CI Pipeline Rewrite** | AI reviewer only read patch hunks, ignored unresolved threads, deferred to other bots, couldn't resolve conversations. Rewrote to fetch full file content, paginate all threads, review independently, strip collapsed HTML, generate structured thread verdicts, and auto-resolve addressed threads via GraphQL. |

### Files added/modified
- `optimization/decision_log.py` — NEW (decision entry dataclass, ring buffer, reason generator)
- `frontend/power-sync-savings.js` — NEW (Lovelace card)
- `optimization/coordinator.py` — savings store, period rollups, cumulative totals, decision log integration, notification scheduler
- `sensor.py` — 8 sensors + 3 sensor classes
- `const.py` — sensor type constants, config keys
- `automations/actions.py` — `_send_savings_notification()` dual-channel wrapper
- `__init__.py` — decision log API endpoint, card registration, entity cleanup
- 7 workflow files — CI/review pipeline fixes

---

## Phase 2 — Reliability (Weeks 5-10) `PLANNED`

**Goal**: Fix the bugs that erode confidence. Stable dashboard, smooth onboarding, reliable battery control.

| Area | Problem | Improvement |
|------|---------|-------------|
| Dashboard stability | Auto-generated dashboard breaks on entity changes | Fix entity detection, graceful fallbacks for missing sensors |
| Config flow | Checkbox bugs, manual entity entry, confusing setup | Auto-detect entities from battery system, validate on save |
| Force discharge | Unreliable execution, silent failures | Better fallback strategies, transparent status reporting, retry with backoff |
| EV charging stats | Incorrect/missing session data | Fix charge session tracking, accurate kWh/cost per session |

**Effort**: MEDIUM | **Impact**: HIGH

---

## Phase 3 — Quick Sensors (Weeks 11-14) `PLANNED`

**Goal**: Low-hanging fruit — valuable sensors and automations that are mostly wiring up existing data.

| Area | Problem | Improvement |
|------|---------|-------------|
| Weather automations | `weather.py` exists but not surfaced | Configurable automations (storm watch, cloud cover adjustments) |
| SOC range limits | No user-visible min/max SOC entities | Expose SOC range as number entities users can adjust |
| Carbon tracking | No environmental impact visibility | CO2 avoided sensor using AEMO NEM emissions data |
| CT diagnostics | Power flow anomalies from bad CT placement go undetected | Anomaly detection sensor flags impossible readings |

**Effort**: LOW | **Impact**: MEDIUM

---

## Phase 4 — Smarter Forecasting (Weeks 15-22) `PLANNED`

**Goal**: Replace the static load estimator with ML that learns from your actual home.

| Area | Problem | Improvement |
|------|---------|-------------|
| Load forecasting | Static estimator doesn't learn usage patterns | scikit-learn model trained on HA history, auto-calibrates against actuals |
| Demand charge protection | No awareness of demand charge windows | Hard grid import block during demand periods, pre-charge before demand windows |
| Forecast accuracy | No feedback loop on prediction quality | Track forecast vs actual, auto-tune model weights |

**Effort**: MEDIUM | **Impact**: HIGH

---

## Phase 5 — EV Expansion (Weeks 23-30) `PLANNED`

**Goal**: Unlock the entire non-Tesla EV market with brand-agnostic charging.

| Area | Problem | Improvement |
|------|---------|-------------|
| OCPP support | Only Tesla EVs supported natively | OCPP protocol for any OCPP-compliant charger |
| Zaptec integration | Zaptec API exists but inactive | Activate Zaptec cloud API for charger control |
| Solar-surplus charging | No solar-aware EV throttling | Auto-throttle EVSE amps to match available solar surplus |
| Multi-vehicle queuing | Single EV assumption | Priority queue for multiple vehicles with configurable rules |

**Effort**: MEDIUM | **Impact**: HIGH

---

## Phase 6 — Battery Intelligence (Weeks 31-36) `PLANNED`

**Goal**: Make the optimizer battery-health-aware and VPP-profitable.

| Area | Problem | Improvement |
|------|---------|-------------|
| Degradation-aware LP | LP solver ignores battery wear costs | Degradation cost penalty using TEDAPI health data in the LP objective function |
| Optimization profiles | One-size-fits-all optimization | "Max savings" vs "battery longevity" profiles — user chooses the tradeoff |
| VPP optimization | VPP events are reactive, not planned | Detect upcoming VPP events, pre-charge, track per-event earnings |

**Effort**: MEDIUM | **Impact**: MEDIUM

---

## Phase 7 — Broader Ecosystem (Weeks 37-48) `PLANNED`

**Goal**: Expand beyond batteries to HVAC and beyond the current pricing providers.

| Area | Problem | Improvement |
|------|---------|-------------|
| HVAC integration | HVAC runs blind to pricing | Pre-cool/heat during cheap periods, starting with Daikin via HA |
| More pricing providers | Limited to Amber, Octopus, Flow Power | Add Energy Australia, AGL, LocalVolts, Origin |
| Battery brand expansion | Community requests for unsupported brands | Prioritize based on demand and contribution |

**Effort**: HIGH | **Impact**: HIGH
