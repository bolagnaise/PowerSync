# EV Charging Refactor Plan

PowerSync currently has several EV charging modes that can independently decide
to start, stop, or adjust the same charger. Discord issue reports show the
biggest failures are not only optimizer math problems; they are ownership,
state, and status consistency problems across Home Assistant and the mobile app.

## Goals

- One source of truth for each EV/loadpoint state.
- One coordinator that arbitrates Smart Schedule, Solar Surplus, Price-Level,
  Scheduled Charging, and manual/app commands.
- PowerSync only stops charging sessions it owns, unless the user has explicitly
  enabled a mode whose policy is to stop external charging.
- Home Assistant and the mobile app display the same charging status, owner,
  reason, and next action.
- OCPP, generic switch/number chargers, Zaptec, Tesla Fleet, Tesla BLE, and
  Teslemetry Bluetooth use the same control contract, with Sigenergy EVAC/EVDC
  chargers represented through the same loadpoint model.

## Reported Failure Patterns

- EV charging disabled, but PowerSync still stops charging started by another
  controller.
- Solar Surplus starts, then another EV mode decides the vehicle is charging
  without an active PowerSync session and stops it.
- HA says the EV is charging while the mobile app says it is not, or the reverse.
- OCPP/generic chargers are not detected, get stuck in `finishing`, or do not
  record sessions.
- Deleted EVs or chargers keep appearing in Solar Surplus status.
- Price-level recovery or opportunity charging does not start when SoC is
  unknown but price thresholds say it should.
- TOU, NEM region, and tariff windows are shifted or mapped to the wrong region.
- Amber/price APIs are polled too often and hit rate limits.

## Discord Issue Map

- `EV stopped Charging after starting but thinks it charging`: Solar Surplus
  started after sustained surplus, then another path treated the vehicle as
  charging without a PowerSync-owned session and stopped it. Users also reported
  manual charging being stopped and repeated mobile notifications.
- `issues with EV charging`: schedule, Solar Surplus, and manual force paths
  disagreed about whether the car was already charging. Recovery charging below
  the configured price threshold also failed when EV SoC/status data was
  incomplete.
- `EV does not stop charging when plugged in during high prices`: the vehicle
  physically drew charge power during a high-price period while PowerSync's
  in-memory price-level state still thought it was not charging.
- `Error setting up entry PowerSync Flow Power`: OCPP interval setup failed with
  an `async_track_time_interval` local binding error.
- `Incorrect TOU time`: tariff windows were shifted by about 12 hours for a
  Globird/Tesla tariff setup. This belongs to the shared tariff/timezone layer,
  not an individual EV mode.

## Reference Systems

- EVCC models the system around loadpoints: a charger, optional vehicle, meters,
  mode (`off`, `now`, `minpv`, `pv`), connected state, charging state, charge
  power, phases, and thresholds. That maps cleanly to a single PowerSync
  loadpoint endpoint shared by HA and mobile.
- EVCC's PV mode uses explicit enable/disable thresholds and delays. PowerSync's
  Solar Surplus should keep those as first-class state, not hidden timers inside
  individual mode handlers.
- SolarCharger uses a signed `Net Power` feedback loop and configurable return
  codes for connected/charging states. PowerSync needs the same normalization
  layer so Tessie, Teslemetry, BLE, OCPP, generic switch chargers, and Zaptec
  can disagree locally without confusing the coordinator.
- SolarCharger exposes multi-car priority/allocation, minimum current, charger
  current control, and power-monitor duration as explicit charger settings.
  PowerSync should keep physical charger capabilities in vehicle charger config
  and have all modes consume that same contract.

## Target HA Model

Create a normalized EV loadpoint model before any mode logic runs:

```python
EVLoadpointState(
    loadpoint_id: str,
    vehicle_id: str | None,
    charger_type: str,
    connected: bool,
    home: bool | None,
    actual_charging: bool,
    power_kw: float,
    current_amps: int | None,
    target_amps: int | None,
    soc: float | None,
    owner: str | None,
    owner_mode: str | None,
    blocking_reason: str | None,
    last_command: dict | None,
)
```

Adapters should translate each charger/provider into this model:

- Tesla Fleet / Teslemetry
- Tesla BLE / Teslemetry Bluetooth
- OCPP through HACS OCPP
- Generic switch/number chargers
- Generic charger fallback SoC sensors, where charger state and vehicle battery
  level come from different integrations
- Zaptec standalone
- Sigenergy EVAC / EVDC Modbus chargers

When multiple Tesla Fleet vehicles use ESPHome BLE bridges in `Both` mode, configure each bridge explicitly as `VIN=prefix` in the EV charging settings.
PowerSync may infer the bridge only when exactly one Fleet VIN and one BLE prefix are configured.
Unmapped or ambiguous vehicles stay on Fleet control so telemetry and commands cannot be attached to another vehicle by discovery order.

## Coordinator Rules

1. Read all loadpoint states.
2. Read normalized prices, tariff periods, solar forecast, grid import/export,
   home battery state, and user mode settings.
3. Produce a desired state for each loadpoint.
4. Apply one command per loadpoint per cycle.
5. Persist ownership leases with `owner`, `owner_mode`, `started_at`,
   `expires_at`, and `session_id`.
6. Never stop an unowned session when EV control is disabled.
7. Never let Price-Level stop Solar Surplus, Scheduled Charging, or Smart
   Schedule unless the coordinator transferred ownership first.
8. Clean up sessions, timers, and mobile-visible state when a vehicle/charger is
   deleted or integration reloads.

## Price And Solar Inputs

- Add a single price provider cache with throttling and backoff.
- Normalize all provider timestamps to timezone-aware datetimes.
- Normalize NEM region from network names such as Energex before tariff lookup.
- Keep negative price, recovery price, opportunity price, and export price logic
  in one decision function.
- Use signed grid power consistently: import positive, export negative.
- Include EV power and battery charge/discharge in solar surplus calculations so
  EV charging does not steal battery-discharge energy and call it surplus.

## Mobile App Changes

- Replace per-screen derived charging truth with the same loadpoint endpoint HA
  uses.
- Show owner/mode, actual charging power, target amps, blocking reason, and next
  evaluation time on every EV screen.
- Remove local success fallbacks for EV settings that can fail in HA.
- Add a diagnostics drawer for raw provider status, normalized loadpoint state,
  last decision, last command result, and session ID.
- After vehicle or charger deletion, force refresh and hide stale dynamic state.

## Per-Phase Mains Protection

Home Power Setup can optionally apply a final per-phase clamp to every
PowerSync-owned EV charging rate. The feature is disabled by default so existing
installations retain the aggregate site-import behaviour until explicitly
enabled.

For a three-phase site, configure the main breaker rating, a safety margin, and
one Home Assistant current entity for each of L1, L2, and L3. These entities
must report electric current and remain fresh. A three-phase charger consumes
the same allocated amps on every phase; a single-phase charger with no known
phase assignment is conservatively counted against every monitored phase.

The controller subtracts only fresh, observed current attributable to a
PowerSync-owned loadpoint from the site phase readings. It never credits a
requested setpoint. All owned loadpoints share one serialized allocation so
they cannot claim the same headroom, and reductions are applied before
increases. The existing aggregate import limit remains an additional cap.

Missing, invalid, unavailable, wrong-unit, or stale required telemetry blocks
new starts and increases. Once telemetry is stale, active PowerSync-owned
charging is stopped. Manual and externally managed charging is never altered;
its measured current continues to consume the available phase budget.

## Acceptance Tests

- When price-level charging is disabled and an EV is externally charging,
  PowerSync sends no stop command.
- When Solar Surplus owns a session, Price-Level does not stop it.
- When Price-Level owns a session and price rises above threshold, PowerSync
  stops that owned session.
- When price-level charging is enabled and an unmanaged Tesla auto-starts during
  high prices, policy enforcement still stops it.
- OCPP `charging -> finishing -> idle` transitions clear active session state.
- Deleting an EV clears dynamic EV state, session tracking, and mobile status.
- HA and mobile return the same `actual_charging`, `power_kw`, `owner_mode`, and
  `blocking_reason` for the same loadpoint.
- Amber/price providers do not exceed their configured polling cadence.
- TOU/tariff windows remain correct across local timezone boundaries.

## First Patch

The first ownership/control patches are intentionally small:

- Price-Level no longer stops a vehicle when Price-Level is disabled.
- Price-Level no longer stops a session already owned by another active dynamic
  PowerSync mode such as Solar Surplus.
- Enabled Price-Level still preserves high-price enforcement for unmanaged Tesla
  auto-start charging.
- EV amperage handling now preserves explicit `0` values instead of treating
  them as missing.
- Dynamic battery-target mode now sets amps through the charger abstraction, so
  OCPP and generic chargers do not fall back to Tesla-only amperage controls.
- HACS OCPP current-limit number entities are used when no built-in OCPP server
  current-control path is available.
- Stopping an untracked dynamic EV session is passive by default and only sends
  a downstream stop command when the caller explicitly marks the stop as owned.
- Zaptec/OCPP interval registration now uses local aliases for Home Assistant's
  interval helper to avoid setup-time scoping failures.
- OCPP status handling now normalizes HACS variants such as
  `Suspended_EVSE`, detects `*_status_connector` consistently, and closes
  sessions once a charger reaches `finishing` with no remaining charge power.
- EV automation live-status inputs now normalize coordinator kW values to watts
  before surplus and battery-target calculations run.
- Physical EV charging detection now treats charger power sensors as a fallback
  truth source, so high-price enforcement can still stop a car that is drawing
  power even when the vehicle API reports a stale non-charging state.
- HA now exposes `/api/power_sync/ev/loadpoints/status` as the first normalized
  loadpoint endpoint, merging PowerSync dynamic state with observed Tesla,
  Zaptec, and OCPP charger telemetry.
- The mobile EV Charging screen now reads that endpoint and shows owner/mode,
  measured power, target amps, site surplus, and blocking reason in one live
  loadpoint card.
- Generic switch/number chargers now appear in the same normalized loadpoint
  endpoint, including configured idle chargers and `commanded_no_power` state
  when amps are set but no charging status is observed.
- Deleting an EV/charger config now clears matching dynamic EV state, active
  sessions, Auto Schedule runtime state, cached SoC, and Price-Level runtime
  state so stale vehicles stop appearing in mobile status.
- Auto Schedule now passes the original vehicle id and synced generic/OCPP
  charger entities into dynamic charging, avoiding `_default` ownership for
  non-Tesla chargers.
- TOU period matching now goes through a shared helper that respects HA/Tesla
  local time, Tesla weekday numbering, minute boundaries, midnight, and
  overnight windows before EV price decisions read the current tariff.
- Smart Schedule now treats empty per-day maps as explicit clears instead of
  falling back to legacy `departure_time` / `departure_days`, and the mobile
  day clear controls save the cleared day immediately rather than sending stale
  React state.
- Price-Level now treats unknown EV SoC as eligible for a recovery-price
  fallback once the vehicle is home and plugged in, so incomplete EV status no
  longer blocks charging below the configured recovery price.
- Amber current and 5-minute price data stay fresh, while the extended
  30-minute forecast is cached for its own refresh window and reused after
  transient failures to reduce API pressure on every EV decision cycle.
- Manual mobile/app Start now records a `manual` PowerSync EV owner after the
  physical command succeeds, clearing any previous mode timer without stopping
  the charger. Manual Stop clears the tracked owner after the physical stop, so
  automation loops do not restart or stop the user's direct command. Manual
  ownership now resolves to the actual loadpoint id (`zaptec_standalone`,
  `generic_ev`, or the Tesla VIN) instead of always falling back to a Tesla-like
  default.
- EV ownership is now mirrored into an explicit per-entry ownership map with
  `owner`, `owner_mode`, `session_id`, and `last_command`, and the normalized
  loadpoint endpoint reads that map before falling back to legacy dynamic state.
- Dynamic EV charging now separates `dynamic_mode` (the amperage control
  algorithm, such as `battery_target`) from `owner_mode` (the business owner,
  such as `smart_schedule`, `scheduled`, or `price_level_recovery`). Zaptec
  standalone starts/stops are also mirrored into the ownership map, so mobile
  status can show who last commanded the charger even when the path bypasses
  the generic dynamic controller.
- EV ownership now has a first-pass per-loadpoint arbiter. Automated starts are
  blocked when another owner family already controls the same loadpoint, while
  same-family updates such as `price_level_recovery` to
  `price_level_opportunity` refresh ownership without restarting the charger.
  Manual ownership remains a hard guard against automated takeovers, including
  direct Zaptec standalone starts.
- EV ownership runtime state now persists last-command diagnostics and a
  snapshot of active ownership. On HA restart, active leases are intentionally
  cleared because timers/control loops do not survive the restart; the previous
  owner is kept as `ev_recovered_ownership` and each loadpoint gets a
  `ha_restart_recovery` last command so the app can show why ownership was
  released instead of leaving a ghost lock.
- The legacy optimizer EV coordinator now also goes through the ownership guard:
  it cannot start while another EV owner family controls a loadpoint, it claims
  ownership only after a physical start succeeds, and it refuses to stop
  charging it does not own.
- Direct OCPP charger start/stop endpoints now record manual ownership and
  release commands for both the raw charger id and `ocpp_` vehicle id alias, so
  mobile-triggered OCPP commands are visible to the same loadpoint status and
  arbitration layer as the broader vehicle command endpoint.
- EV Boost now uses the requested duration as an actual stop timer, records a
  `boost` ownership lease, applies an optional target SoC when supported, and
  skips the automatic stop if another owner has taken control before the boost
  window expires.
- Direct Zaptec stop paths in Price-Level, Scheduled Charging, and the combined
  EV mode coordinator now check the active ownership family before sending a
  physical stop command, preventing stale executor state from stopping manual or
  other-mode Zaptec charging.
- User-created HA automation actions that call `start_ev_charging` directly now
  record manual ownership, while direct `stop_ev_charging` clears that ownership
  after the physical stop. Internal callers such as EV Boost can opt out and
  claim their own owner mode.
- The normalized loadpoint status merger now applies ownership and last-command
  aliases to observed-only chargers too, so OCPP ids such as `evse_1` and
  `ocpp_evse_1` resolve to the same mobile-visible owner/status record.
