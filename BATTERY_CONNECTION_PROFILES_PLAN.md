# Battery Connection Profiles Migration Plan

Date: 2026-08-13

## Decision

PowerSync should add a brand-specific **Connection method** option, but it should
not expose independent, free-form telemetry and control selectors.

Internally, telemetry and each control operation must be routed separately.
Externally, users choose one validated profile bundle for their battery system.
The profile states which source owns telemetry, which source owns each supported
control, whether PowerSync opens a hardware connection, and how commands are
verified and restored.

This solves the duplicate-connection problem without allowing unsafe source
combinations. An entity-backed profile must create no PowerSync-owned direct
client, probe, health check, reconnect task, or fallback connection.

## Feature contract

- Existing configurations keep their current route and behavior after migration.
- Existing PowerSync entity IDs, unique IDs, services, API fields, and normalized
  sensor meanings remain unchanged.
- A user can select only profiles registered and validated for the chosen brand.
- PowerSync resolves the selected profile before importing or constructing a
  provider client.
- Entity-backed profiles reference an upstream Home Assistant config entry and
  device; credentials are never copied from that integration.
- Missing, unavailable, malformed, or stale upstream data fails closed. It never
  becomes zero and never triggers a direct fallback.
- Unsupported controls are removed from planning as well as blocked at execution.
- A route may advertise a control only when it has capture, apply, authoritative
  readback, verification, ownership, and restore semantics.
- Changing profile restores any PowerSync-owned temporary state through the old
  route before unloading it. An unresolved restore blocks the switch.
- Monitoring mode performs no writes through direct or entity-backed routes.

## Architecture

Add a small package under `custom_components/power_sync/battery_backend/`:

- `types.py`: operations, route kinds, support/verification/restore enums,
  capabilities, connection footprints, and profile specifications.
- `profiles.py`: a pure, static registry containing all supported systems and
  stable profile IDs. It must not import coordinators or Modbus clients.
- `backend.py`: the lazy backend factory plus a compatibility facade exposing the
  normalized coordinator surface already consumed by PowerSync.
- `lifecycle.py`: startup/shutdown, direct-resource leases, reconnect cancellation,
  source freshness, restore-before-switch, and ownership handoff.
- `migration.py`: config schema versioning and deterministic legacy-config-to-profile
  mapping.
- `sensor_types.py`, `sensor_specs.py`, `sensor_discovery.py`, and
  `sensor_catalog.py`: the read-only upstream sensor catalog described in
  [BATTERY_SENSOR_DISCOVERY_RESEARCH.md](BATTERY_SENSOR_DISCOVERY_RESEARCH.md).

Persist only additive routing data:

```yaml
connection_profile_version: 1
connection_profile_id: sungrow_ha_integration
source:
  config_entry_id: upstream-entry-id
  device_id: upstream-device-id
entity_overrides: {}
sensor_display_mode: recommended
sensor_include_overrides: []
sensor_exclude_overrides: []
```

Keep direct credentials and existing immutable installation details in their
current fields. Keep mutable route selection and upstream references in options.
Do not delete legacy fields during the initial migration period.

The resolved runtime object should contain:

- the normalized telemetry source;
- operation-level control bindings;
- runtime capabilities;
- sanitized connection footprints;
- route and readiness diagnostics;
- an awaited shutdown method.

Legacy coordinators can initially sit behind a compatibility facade. New
entity-backed routes must implement an explicit interface and must never delegate
missing operations to a direct coordinator.

The first integration change in `__init__.py` should be limited to resolving the
profile, calling one lazy backend factory, assigning the returned facade, and
awaiting its shutdown. The optimizer, platforms, services, and entity creation
should not be reorganized as part of this work.

## Capability model

Register at least these operations independently:

- force charge;
- force discharge;
- restore normal;
- backup reserve;
- idle/no-discharge;
- charge rate;
- discharge rate;
- export limit;
- curtailment and curtailment restore.

Each capability records:

- support: native, safely emulated, monitoring-only, or unsupported;
- route ID;
- authoritative readback source;
- verification mode and timeout;
- restore mode;
- whether exact pre-state capture exists;
- whether persistent ownership is required;
- current runtime availability.

Mixed systems use named profiles with operation-level bindings. Users never build
their own arbitrary combinations.

## Supported-system matrix

| System | Current route(s) | Target registered profiles | Migration disposition |
|---|---|---|---|
| Tesla | Fleet API, Teslemetry, PowerSync cloud/provider paths | Existing provider profiles; validated HA profile; only validated hybrids | Preserve Tesla-specific semantics. Do not present it as a Modbus-style choice. HA controls are advertised only with reliable readback and restore. |
| Sigenergy | Direct Modbus for live telemetry/control; cloud for tariff/app data | Direct Modbus plus cloud auxiliary; HA control profile if complete; HA monitoring profile otherwise | An HA profile must suppress every PowerSync Modbus path. Cloud is never control telemetry. |
| Sungrow | Direct Modbus; iHomeManager-forwarded Modbus telemetry-only route | Direct inverter; direct iHomeManager; HA integration; HA monitoring if control parity is incomplete | Highest-priority new entity route after the framework pilots. Zero PowerSync Modbus construction in either HA profile. |
| FoxESS | TCP, serial, cloud, `foxess_modbus` entities | `foxess_direct_tcp`, `foxess_direct_serial`, `foxess_cloud`, `foxess_ha_modbus` | First reference implementation; map existing options one-to-one. |
| GoodWe | Direct/library path, upstream telemetry, EMS entity control | Validated telemetry-plus-EMS composite; monitoring-only; complete direct only where safe | Validate that telemetry and EMS controls belong to the same installation. No arbitrary pairing. |
| AlphaESS | Modbus plus optional cloud; cloud-only monitoring | Direct Modbus plus cloud auxiliary; cloud monitoring; validated HA profile | Cloud-only remains read-only. Cloud fallback cannot substitute for fresh local control telemetry. |
| ESY Sunhome | Companion HA integration | Existing HA profile | Register and normalize current behavior. Do not invent a second direct implementation for symmetry. |
| SolaX | `solax_modbus` HA integration | Existing HA profile | Register current bridge and validate signs, units, freshness, modes, and restore. |
| SAJ H2 | `saj_h2_modbus` HA integration | Existing HA profile | Register current bridge and make capability gaps explicit. |
| Fronius GEN24/Reserva | Fronius Modbus HA integration | Existing HA profile | Preserve the bridge and verify battery/control identity and authoritative mode restoration. |
| Neovolt | Neovolt HA integration | Existing HA profile | Register current bridge and report actual control coverage. |
| SolarEdge | HA entity telemetry/control plus direct Modbus curtailment | Legacy composite; HA-only; full direct only if complete | HA-only must disable curtailment planning when unavailable, never start Modbus as a fallback. |
| Anker Solix | Direct X1 Modbus; official HA; unofficial HA | `anker_direct_modbus`, `anker_ha_official`, `anker_ha_unofficial` | Second reference implementation; keep official and unofficial adapters separate. |
| Custom/external | User-selected HA telemetry, planner only | Existing external monitoring/planner profile | Keep read-only. Do not add generic arbitrary write entities. |

A system with only one safe route is still fully migrated to the profile model.
The UI must not advertise an unsafe second route solely for visual symmetry.

## Automatic sensor discovery and display

The connection profile also defines the safe discovery boundary for richer
upstream telemetry. PowerSync will keep its existing normalized sensors and add a
versioned catalog that references source entities for the dashboard. It will not
clone every upstream sensor into a new recorded PowerSync entity, and it will
never infer controls from sensor discovery.

The researched per-brand integration matrix, config-entry/device discovery rules,
Sungrow YAML fallback, dashboard design, duplicate handling, lifecycle, and test
gates are in
[BATTERY_SENSOR_DISCOVERY_RESEARCH.md](BATTERY_SENSOR_DISCOVERY_RESEARCH.md).

Sensor discovery is part of each connection-profile adapter, not an independent
whole-instance scan. A profile is incomplete until it declares its sensor adapter
or an explicit manual-only policy.

## Delivery slices

Patch versions below are the intended sequence after v2.12.1089; the actual
version is assigned only when that slice passes its release gates.

### Slice 1: shared framework, FoxESS, and Anker (target v2.12.1090)

1. Add characterization tests for all legacy configuration shapes, normalized
   telemetry, current controls, monitoring mode, unload/reload, entity identity,
   and direct-client construction.
2. Add all 14 systems and their legacy-equivalent profiles to the static registry.
3. Bump the config-entry schema and add deterministic, additive migration.
4. Add the lazy backend factory, compatibility facade, capabilities, lifecycle,
   direct-resource leases, and diagnostics.
5. Add the catalog types, versioned API contract, dashboard System details shell,
   and registry/state lifecycle with no brand enabled.
6. Resolve the profile before the existing brand construction block.
7. Add the options-flow selector, sensor display mode, and preflight summary.
8. Migrate FoxESS, then Anker, to selectable profile factories and sensor adapters.

Release gates:

- every existing entry resolves to its exact current route;
- no entity or config-entry identity changes;
- all non-pilot providers retain current behavior;
- FoxESS exposes four profiles and Anker exposes three;
- HA profiles construct and connect zero direct clients, including after reload;
- stale or missing entity data does not fall back;
- profile switching restores the old route before saving;
- unload closes clients and cancels reconnect work;
- route diagnostics match runtime reality;
- catalog discovery stays inside the selected upstream device graph;
- canonical sensors are not duplicated and no extra recorded entities are made;
- discovered write entities never become controls;
- focused tests and the full Python 3.12 suite pass.

### Slice 2: Sungrow (target v2.12.1091)

Add the direct inverter, direct iHomeManager, HA integration, and—if required by
upstream capability gaps—explicit HA monitoring profiles. Preserve the current
direct profile for every existing entry.

Map normalized solar, grid, battery, load, SOC, modes, limits, and all controls
that meet the full apply/readback/restore contract. Controls that do not meet it
remain unsupported; they must not invoke direct Modbus.

Add the bounded Sungrow YAML sensor adapter: require an anchor, validate known
unique-ID/platform patterns, preview its scope, and reject ambiguous multi-inverter
matches. Do not generalize this into friendly-name scanning.

Release gates include zero Sungrow direct-client construction/connect/reconnect
under HA profiles across initial setup, stale upstream data, entry reload, and HA
restart. The existing v2.12.1089 direct reconnect behavior must remain unchanged.

### Slice 3: AlphaESS and existing entity bridges (target v2.12.1092)

Migrate in this order:

1. AlphaESS direct-plus-cloud and cloud-only semantics;
2. ESY Sunhome;
3. SolaX;
4. SAJ H2;
5. Fronius GEN24/Reserva;
6. Neovolt;
7. Custom/external.

This slice normalizes existing bridges under the common contract. It does not add
new direct protocols or generic custom write controls. It also adds their sensor
catalog adapters, including SAJ fast/history deduplication and Neovolt parallel
device grouping.

### Slice 4: GoodWe and SolarEdge composites (target v2.12.1093)

Implement operation-level route binding and same-installation validation.

- GoodWe: bind upstream telemetry and EMS entity controls as one validated
  composite; offer monitoring-only when EMS controls are absent.
- SolarEdge: preserve the legacy entity-plus-direct-curtailment composite and add
  HA-only. HA-only reports curtailment unsupported instead of opening Modbus.
- Add the Home Assistant GoodWe and SolarEdge Modbus Multi sensor adapters. Scope
  SolarEdge discovery to detected inverter/meter/battery children and keep packs
  separate.

### Slice 5: Tesla, Sigenergy, and cross-brand completion (target v2.12.1094)

- Tesla: represent Fleet, Teslemetry, PowerSync/cloud, local API, validated HA,
  and any explicitly verified hybrids without flattening provider semantics.
- Sigenergy: keep direct Modbus live control separate from auxiliary cloud data;
  expose HA control only where upstream apply/readback/restore is complete, plus
  monitoring-only where it is not.
- Add local Powerwall gateway/pack and Sigenergy plant/inverter/charger sensor
  adapters with explicit source attribution and topology.
- Finish the all-brand capability matrix, options UX, diagnostics, migration
  coverage, and zero-direct-client contract tests.

## Safe profile-switch sequence

1. Resolve and read-only preflight the new profile.
2. Validate upstream config entry, device identity, required entities, freshness,
   units, signs, and control/readback coverage.
3. Stop new optimizer actions.
4. Restore any temporary state through the old route.
5. Verify restoration and clear ownership.
6. Await old-route shutdown, direct-client close, reconnect cancellation, and
   resource-lease release.
7. Save the new profile and reload the same PowerSync config entry.
8. Wait for a fresh identity-validated snapshot.
9. Enable only the new route's verified capabilities.

If preflight or restoration fails, do not save the change. If new-route startup
fails after a safe handoff, leave the entry unavailable and surface a repair issue;
never silently reactivate direct hardware access.

## Safety and failure thought experiments

- **Upstream integration disappears:** mark telemetry stale/unavailable, block
  control and planning, keep all direct factories untouched, and recover only
  after a fresh validated snapshot.
- **Entity service returns success but hardware does not change:** verification
  fails, bounded restoration runs through the same route, and ownership remains
  visible until cleanup is verified.
- **Profile changes during force mode:** restore using the old owner first. An
  unresolved state blocks switching.
- **SolarEdge HA-only lacks curtailment:** capability is unsupported and the plan
  cannot emit curtailment; no specialty Modbus client starts.
- **GoodWe telemetry and EMS entities point to different devices:** preflight
  rejects the profile rather than guessing.
- **Entity IDs are renamed:** resolve primarily through config entry, device
  registry, and entity registry unique IDs; entity IDs are explicit overrides.
- **Two PowerSync entries target one direct endpoint:** the second cannot acquire
  the sanitized connection-footprint lease and fails closed.
- **An upstream automation fights PowerSync:** authoritative readback divergence
  is ownership loss/control failure, not a reason for unbounded repeated writes.

## Tests

Add focused shared tests:

- `tests/test_battery_profile_registry.py`
- `tests/test_battery_profile_migration.py`
- `tests/test_battery_backend_factory.py`
- `tests/test_battery_backend_lifecycle.py`
- `tests/test_battery_capability_contract.py`
- `tests/test_battery_route_diagnostics.py`
- `tests/test_battery_entity_identity.py`

The shared contract must test:

- all 14 systems are registered;
- every legacy configuration maps deterministically;
- profile IDs are stable and invalid combinations are rejected;
- third-party profiles call direct constructors, connects, and reconnect schedulers
  zero times, including after reload/restart paths;
- cloud auxiliary data cannot become control telemetry;
- stale/unavailable/malformed data fails closed;
- W/kW, SOC fraction/percentage, and brand-specific power signs normalize correctly;
- unsupported capabilities cannot be planned or executed;
- monitoring mode writes nothing;
- apply without authoritative readback is failure;
- failed apply/verify attempts bounded same-route restoration;
- unresolved cleanup survives restart;
- unload cancels subscriptions, timers, direct clients, and reconnect work;
- profile switching restores the old route before activation;
- diagnostics contain no credentials or raw private connection details.
- sensor discovery is restricted to the selected integration/device graph;
- optional/disabled/stale/missing catalog metrics are represented honestly;
- discovery creates no duplicate Home Assistant entities or recorder statistics;
- catalog roles cannot advertise or execute writes;
- registry rename/add/remove and upstream unload/reload update the catalog without
  duplicate subscriptions;
- multi-device sites and live/history duplicates follow adapter policy.

Run each slice's new tests plus the adjacent existing brand regressions, then:

```bash
rtk python3.12 -m pytest -q
```

## Sungrow live validation

1. On the existing config entry, record direct-route normalized telemetry, entity
   IDs, work mode, limits, ownership state, and one active PowerSync client.
2. Confirm the upstream Sungrow HA integration is fresh and select its exact config
   entry/device in PowerSync.
3. Switch profiles through the safe handoff sequence.
4. For at least three upstream update intervals, prove normalized telemetry updates
   while direct construction, connect attempts, and reconnect tasks remain zero.
5. Reload the PowerSync entry and repeat the zero-direct checks.
6. Restart Home Assistant and repeat them again.
7. Unload the upstream integration; confirm stale/unavailable PowerSync telemetry,
   no writes, and no direct fallback. Reload it and require a fresh snapshot.
8. With explicit live-control authorization, perform one bounded reversible control
   that has authoritative HA readback, restore the exact prior value, and verify
   ownership cleanup.
9. Restore normal through the HA route, switch back to direct, and prove exactly one
   direct client plus unchanged PowerSync entity IDs.

## Highest-risk false completion

The feature can appear correct while still causing contention if the legacy direct
coordinator is constructed before profile resolution and then hidden behind an HA
facade. The UI and sensors would use entities while the unused coordinator keeps
polling or reconnecting in the background.

The mandatory order is:

```text
resolve profile -> select one lazy factory -> construct only that backend
```

Every HA-profile test must patch the direct client constructor itself and assert
zero constructor calls, zero connect calls, and zero reconnect tasks through setup,
failure, unload/reload, and restart recovery.

## Definition of done

- All 14 supported systems have stable registered profiles and capability data.
- All legacy entries preserve behavior and public entity identity.
- Every currently feasible direct, cloud, and third-party route is represented.
- Mixed systems use validated named composites with operation-level binding.
- Every advertised control has safe capture/apply/verify/restore semantics.
- Third-party profiles provably open no PowerSync hardware connection.
- Capabilities constrain planning and execution.
- Profile changes safely hand ownership from old route to new route.
- Focused and full Python 3.12 tests pass.
- Runtime validation proves Sungrow entity routing, direct-client suppression,
  stale-source behavior, reload/restart cleanup, and contention avoidance.
- Every system has a researched primary sensor integration and a tested discovery
  adapter or an explicit manual-only policy.
- Recommended upstream metrics display automatically without changing canonical
  PowerSync sensors, duplicating recorder data, or exposing generic controls.
