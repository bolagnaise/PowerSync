# Battery Integration Sensor Discovery and Display Plan

Date: 2026-08-13

Companion to [BATTERY_CONNECTION_PROFILES_PLAN.md](BATTERY_CONNECTION_PROFILES_PLAN.md).

## Decision

PowerSync should automatically discover the richer sensor surface exposed by the
selected battery integration and display it in the PowerSync dashboard. It should
not mirror every upstream entity into a second PowerSync entity.

The existing normalized PowerSync sensors remain the stable public and optimizer
contract. A new read-only sensor catalog references the original Home Assistant
entities for system details. This avoids duplicate recorder data, duplicate entity
clutter, ambiguous ownership, and a second source of truth.

Only a curated canonical role may become a PowerSync entity. Discovery must never
promote an arbitrary upstream `number`, `select`, `switch`, `button`, or service
into a PowerSync control. Controls continue to come only from the validated
connection profile capability contract.

## User-facing contract

- The current Overview and existing PowerSync entity IDs do not change.
- A new **System details** view groups discovered values by physical device and
  function: battery, solar strings, grid/phases, inverter/backup, energy totals,
  temperature/health, status/faults, and optional charger/auxiliary devices.
- The default display mode is **Recommended**. Users may select **All supported**
  or **Off**, then include or exclude individual optional metrics.
- PowerSync displays source integration, source device, native unit, availability,
  and update age. A stale or unavailable value is never displayed as zero.
- Disabled upstream entities are shown in the setup preview as available but
  disabled. PowerSync does not silently enable them.
- Renaming an entity does not lose the selection when its registry unique ID is
  unchanged.
- Multi-inverter and multi-battery sites remain visibly separated. Site aggregates
  are not silently mixed with pack or inverter measurements.
- PowerSync never scans unrelated integrations or globally matches friendly names.

## Architecture

Add these responsibilities under `custom_components/power_sync/battery_backend/`:

- `sensor_types.py`: immutable role, category, source identity, freshness, and
  display metadata types.
- `sensor_specs.py`: static per-integration role aliases, category metadata,
  recommendation priority, and duplicate-resolution rules.
- `sensor_discovery.py`: config-entry/device graph discovery plus the explicit
  legacy/YAML fallback path.
- `sensor_catalog.py`: catalog lifecycle, registry subscriptions, state lookup,
  deduplication, and versioned API serialization.

Suggested core types:

```python
@dataclass(frozen=True)
class SensorRoleSpec:
    role: str
    category: str
    unique_id_suffixes: tuple[str, ...]
    device_classes: tuple[str, ...] = ()
    state_classes: tuple[str, ...] = ()
    recommended: bool = False
    source_priority: int = 0
    historical: bool = False

@dataclass(frozen=True)
class DiscoveredMetric:
    source_entity_id: str
    source_unique_id: str | None
    source_domain: str
    source_config_entry_id: str | None
    source_device_id: str | None
    physical_device_key: str
    role: str
    category: str
    device_class: str | None
    state_class: str | None
    native_unit: str | None
    recommended: bool
    enabled: bool
    available: bool
    stale: bool
    last_updated: datetime | None
```

The stable identity key is:

```text
integration domain + upstream config entry + upstream device + entity unique ID
```

For integrations without config entries or device-registry ownership, the stable
fallback key is:

```text
selected anchor entity + known integration family + entity unique ID
```

Entity IDs are display pointers and explicit overrides, not primary identity.

## Discovery algorithm

### Config-entry integrations

1. Start with the upstream config entry and physical device selected by the
   connection profile. Never start with all Home Assistant states.
2. Walk only the permitted device graph: selected site/inverter, its battery,
   meter, PV/string, backup, and explicitly supported child devices connected via
   `via_device_id` or integration-specific topology metadata.
3. Read entity-registry rows owned by that config entry and graph.
4. Match per-integration unique-ID aliases first. Use device class, state class,
   unit, platform, and device relationship as validation, not as a global guess.
5. Resolve duplicate candidates with the adapter's declared priority. If two
   equally authoritative candidates remain, show an ambiguity repair issue and
   require an explicit selection.
6. Keep disabled, unavailable, and stale status in metadata. Do not turn missing
   data into a numeric value.
7. Deduplicate entities already represented by a canonical PowerSync sensor. The
   canonical tile remains once and gains source attribution.

### YAML and legacy integrations

The common Sungrow package is YAML-based and can expose `modbus` platform entities
without a usable upstream config-entry/device graph. It therefore needs a bounded
fallback rather than the generic config-entry algorithm.

1. Require the user to select an anchor entity from the intended inverter, such
   as its device type, serial, inverter power, or battery SOC entity.
2. Validate the entity platform, a known Sungrow unique-ID family, and the expected
   core measurement set.
3. Build a preview from entities sharing the configured prefix and validated
   unique-ID namespace. Do not accept names alone.
4. If multiple inverters share a namespace or the scope cannot be proved, fail
   setup and request explicit anchors for each physical device.
5. Store unique IDs and the anchor identity. Re-resolve current entity IDs on load.

This fallback is adapter-specific. It must not become a generic whole-instance
entity-name scanner.

## Catalog lifecycle

- Build the catalog after the connection profile has validated its upstream
  identity and before exposing it through the API.
- Subscribe to entity-registry updates, device-registry updates, source config
  entry reload/unload, and state changes for catalog entity IDs.
- Recompute a catalog diff rather than reloading PowerSync for ordinary additions,
  removals, disables, or renames.
- Re-resolve an entity rename by unique ID. Mark a removed optional metric absent;
  mark a required canonical role unavailable and fail the active route closed.
- On upstream unload, retain catalog shape long enough to show the source as
  unavailable, unsubscribe state listeners, and never start a direct fallback.
- On reload, require one fresh source snapshot before clearing stale status or
  allowing control.
- Ensure unload removes registry and state listeners so repeated reloads do not
  duplicate callbacks.

## API and dashboard

Expose a backward-compatible, versioned API field; existing consumers can ignore
it:

```json
{
  "battery_sensor_catalog": {
    "version": 1,
    "source": {
      "profile_id": "foxess_ha_modbus",
      "integration": "foxess_modbus"
    },
    "groups": [
      {
        "category": "battery",
        "device_key": "battery_1",
        "metrics": [
          {
            "role": "battery_temperature",
            "entity_id": "sensor.foxess_battery_temperature",
            "recommended": true,
            "enabled": true,
            "available": true,
            "stale": false,
            "last_updated": "2026-08-13T10:00:00+10:00"
          }
        ]
      }
    ]
  }
}
```

The API sends metadata and source entity IDs, not copied state histories. The
dashboard subscribes to those source entities and renders their live Home
Assistant states. For non-live HTTP clients, the API may include the current
native state and unit as a convenience, but the source entity remains canonical.

Dashboard layout:

1. **Overview**: unchanged normalized PowerSync flow, price, SOC, and action plan.
2. **System details**: recommended metrics grouped by physical device/category.
3. **Diagnostics**: status, faults, firmware, update age, disabled entities, and
   source identity; collapsed by default.
4. **Controls**: only controls advertised by the selected connection profile;
   never generated from catalog discovery.

Live/recorder duplicate policy:

- Prefer a fast entity for live power-flow display.
- Prefer a total-increasing/statistics-compatible entity for historical energy.
- Never show both as the same logical metric unless the labels clearly distinguish
  instantaneous and recorded variants.
- SAJ `fast` entities are the required regression case for this policy.

## Researched primary integration matrix

“Primary” here means the integration PowerSync should support first for automatic
discovery, based on current Home Assistant/core availability, the integration
already targeted by PowerSync, local-control suitability, and project maturity.
It does not imply an exclusive endorsement.

| Battery/system | Primary integration profile(s) | Discovery shape | Recommended extra sensor groups |
|---|---|---|---|
| Tesla Powerwall | [Home Assistant Powerwall](https://www.home-assistant.io/integrations/powerwall/) for local gateway telemetry; existing Tesla Fleet/Teslemetry profiles for cloud controls | Config entry with gateway and per-battery devices. Keep local telemetry and cloud control identity explicitly paired. | Gateway solar/grid/load/battery/generator flow; reserve; voltage/current/frequency; lifetime energy; per-pack power, capacity, remaining energy and grid state; firmware and active alerts. PW3 capability gaps must remain explicit. |
| Sigenergy | [Sigenergy Local Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus), domain `sigen` | Plant config entry with inverter and AC/DC charger children. Use plant topology and device IDs; diagnostics and controls can be disabled by default upstream. | Plant power/PV/SOC/grid flow; inverter MPPT, battery SOC/SOH and phase data; energy totals; temperatures; states/faults; optional AC/DC charger group. Do not invent removed/undocumented grid-phase values. |
| Sungrow | [Sungrow SHx Modbus YAML package](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant) first; [Modbus Manager](https://github.com/TCzerny/ha-modbus-manager) as a future config-entry adapter | The common package needs the explicit anchor/unique-ID fallback. Modbus Manager can use normal config-entry/device discovery. | MPPT 1–4 voltage/current/power; phase voltage/current/power; battery voltage/current/temp/SOH/capacity/cell extremes; backup load; daily/total energy; inverter state, device type and firmware. |
| FoxESS | [FoxESS Modbus](https://github.com/nathanmarlor/foxess_modbus), domain `foxess_modbus` | Config entry/device registry with model-specific entities; current PowerSync mappings provide initial unique-ID aliases. | PV 1–6, CT2 and phase values; battery voltage/current/temp/SOH/BMS limits; work mode/reserve/rates; daily/total energy; states and faults. |
| GoodWe | [Home Assistant GoodWe](https://www.home-assistant.io/integrations/goodwe/) | Core config entry/device. Entity availability is model-family dependent, so match roles to the detected model and present absent roles as unsupported. | PV strings and phases; battery voltage/current/temp/SOH; UPS/backup phases; meter/grid values; daily/total energy; operation mode and status. Controls remain tied to the validated EMS route. |
| AlphaESS | [AlphaESS for Home Assistant](https://github.com/CharlesGillanders/homeassistant-alphaESS), domain `alphaess`, for the common API-backed profile; retain PowerSync direct/local profiles separately | Config entry/site and device ownership. Cloud freshness and control availability must be declared independently from local profiles. | Current PV/grid/load/battery flow; SOC/capacity/temp; daily PV, import, export, grid charge, battery charge/discharge and EV energy; schedules, mode, status and alarms. |
| ESY Sunhome | [ESY Sunhome](https://github.com/branko-lazarevic/esysunhome), domain `esy_sunhome` | MQTT-backed config entry/devices. Use registry identity rather than MQTT topic guessing. | DC/AC PV, battery/grid/load; SOC/SOH/voltage/current/status; daily/total generation, consumption, export and battery energy; grid voltage/frequency; inverter temperature and operating mode. |
| SolaX and compatible plugin families | [SolaX Modbus](https://github.com/wills106/homeassistant-solax-modbus), domain `solax_modbus` | Config entry and plugin/model-specific device graph. Record plugin family/model in discovery diagnostics because entity surfaces differ substantially. | PV strings; grid phases; battery/BMS values; EPS/backup; daily/total energy; temperatures; firmware, operation state and faults. |
| SAJ H2 | [SAJ H2 Modbus](https://github.com/stanus74/home-assistant-saj-h2-modbus) | Config entry/device graph with broad register exposure and optional fast entities. Apply explicit fast/live versus recorder deduplication. | PV 1–3; phase values; battery voltage/current/temp/SOH/BMS; work mode and direction; daily/total energy; schedules, state and faults. |
| Fronius GEN24/Reserva | [Home Assistant Fronius](https://www.home-assistant.io/integrations/fronius/) for common read-only Solar API telemetry; [Fronius Modbus](https://github.com/callifo/fronius_modbus) for the PowerSync control profile | Two separate adapters. Core Fronius can discover power-flow, inverter, meter and storage devices; the Modbus entry supplies validated battery controls. Pair only when the physical installation matches. | Site flow; inverter/phase values; meter import/export; storage SOC, voltage/current, capacity and cycle data; energy totals; temperature/status; optional Ohmpilot as a separate auxiliary device. |
| Neovolt/Bytewatt | [Neovolt Battery Modbus Plugin](https://github.com/pvandenh/NeovoltBattery_ModbusPlugin), domain `neovolt` | Config entry with inverter/battery devices and combined entities for parallel installations. Preserve both device and aggregate scope. | Three-phase grid; PV 1–3; battery V/I/P/SOC/SOH/capacity/energy, cell voltage/temp extremes and faults; inverter temperature/bus/backup/work mode; daily/total and combined parallel-system values. |
| SolarEdge | [SolarEdge Modbus Multi](https://github.com/WillCodeForCats/solaredge-modbus-multi), domain `solaredge_modbus_multi` | Hub config entry with 1–32 inverter devices and detected meter/battery children. Battery/control entities are optional upstream and must not be assumed enabled. | Inverter AC/DC/phase/status; meter phases/import/export; up to three batteries per inverter with SOC/V/I/P/temp/energy/status; site limit and storage status. |
| Anker Solix | [Anker SOLIX official](https://github.com/anker-charging/ha-anker-solix-official) for supported local devices; [Anker SOLIX unofficial](https://github.com/thomluther/ha-anker-solix) as a separate cloud profile | Separate domains/adapters and device graphs; never merge by friendly name. Direct X1 Modbus remains a third profile until official local coverage is verified. | Site/device power flow; Solarbank SOC and battery power; PV strings; home/load/grid; daily energy; temperature, status, firmware and diagnostics; cloud-only schedules in the unofficial profile. |
| Custom/external | User-selected Home Assistant device/entities only | No automatic global discovery. User selects a device or bounded entity list; PowerSync validates classes/units and stores registry identities. | Only user-selected read-only metrics. No inferred controls and no cross-device expansion. |

## Per-adapter implementation rules

Every supported adapter declares:

- accepted upstream domains and minimum supported integration version, if known;
- config-entry and device-topology rules;
- canonical role aliases and required validation metadata;
- optional detail roles, display category, recommendation priority, and sensitivity;
- duplicate preferences for aggregate/pack, fast/history, and calculated/raw values;
- required freshness bounds based on the upstream polling cadence;
- roles that are intentionally unavailable for specific models/firmware;
- whether legacy/YAML anchor discovery is allowed;
- controls separately, through the connection profile capability registry.

Raw entity attributes are not copied wholesale. Adapter code must explicitly
allow any attribute shown in the dashboard or diagnostics so credentials, host
details, identifiers, or large payloads do not leak through the catalog API.

## Thought experiments and failure behavior

- **A renamed FoxESS entity:** unique ID resolves the new entity ID and the
  dashboard updates without changing the normalized PowerSync entity.
- **Two identical Sungrow YAML installations:** prefix matching alone is rejected;
  each needs a verified anchor and distinct candidate preview.
- **A Sigenergy firmware omits a register:** the optional metric is absent, not
  zero, and the rest of the plant catalog remains valid.
- **SAJ exposes both normal and fast battery power:** the fast entity drives live
  display; the recorder-compatible entity remains the history source; one logical
  battery-power row is shown.
- **SolarEdge detects three batteries:** render three pack groups plus a site
  aggregate only when the integration actually exposes one; never sum mismatched
  units or signs implicitly.
- **Fronius core telemetry and Modbus control point to different systems:** source
  identity preflight rejects the composite profile before either is activated.
- **An optional diagnostic entity is disabled:** list it as disabled in setup and
  diagnostics, but do not enable it or treat it as an error.
- **An upstream integration unloads:** values become unavailable/stale, control
  fails closed, and no PowerSync direct client is constructed.
- **A malicious or accidental entity name resembles battery SOC:** it is ignored
  unless it is owned by the selected source scope and matches adapter validation.
- **A source exposes an attractive write entity:** it never appears in Controls
  until an adapter capability supplies capture, apply, readback and restore.

## Delivery plan

### Foundation

1. Characterize the existing fixed sensor lists and dynamic Powerwall/string
   entity lifecycle in `sensor.py`.
2. Add catalog types, category vocabulary, API schema, selection persistence, and
   registry/state lifecycle without enabling a brand.
3. Add dashboard System details and Diagnostics views behind catalog version 1.
4. Prove no new Home Assistant entities or recorder statistics are created.

### Reference adapters

1. FoxESS: first config-entry/device-graph adapter because current read mappings
   already cover a broad sensor surface.
2. Anker official and unofficial: prove two integrations for one brand remain
   separate and do not merge devices.
3. Sungrow: prove the bounded YAML anchor fallback and multi-inverter ambiguity
   handling.

### Existing entity-backed adapters

Add ESY, SolaX, SAJ, Fronius Modbus, Neovolt, AlphaESS, GoodWe and SolarEdge.
For Fronius, add Core Solar API monitoring as a distinct adapter. Apply the SAJ
fast/history and Neovolt/SolarEdge multi-device regression cases.

### Final adapters

Add Tesla local Powerwall plus cloud profile attribution, and Sigenergy plant
topology including optional charger groups. Complete model-specific capability
exceptions and the user-facing compatibility matrix.

## Verification gates

Add focused tests such as:

- `tests/test_battery_sensor_catalog.py`
- `tests/test_battery_sensor_discovery.py`
- `tests/test_battery_sensor_api.py`
- `tests/test_battery_sensor_dashboard_contract.py`
- per-brand discovery fixtures beside existing bridge tests.

Required assertions:

- all 14 systems have a primary sensor adapter or an explicit manual-only policy;
- discovery never leaves the selected config-entry/device graph;
- entity rename survives through unique-ID resolution;
- disabled, unavailable, stale, malformed and removed entities remain distinct;
- no missing value is coerced to zero;
- multi-inverter, multi-battery and aggregate entities are not cross-wired;
- Sungrow YAML ambiguity fails closed;
- SAJ fast/history duplicates resolve by use case;
- canonical PowerSync sensors are not duplicated in System details;
- no discovered write entity is promoted to a control;
- catalog state creates no extra Home Assistant entity or recorder statistic;
- registry updates apply one diff without duplicate listeners;
- unload/reload and upstream removal/recovery cleanly unsubscribe and rebind;
- catalog API version 1 is backward-compatible with clients that ignore it;
- redacted diagnostics contain no credentials, host secrets or unsafe attributes;
- direct-client constructor counts remain zero under every entity-backed profile.

Run the focused adapter and dashboard tests on each slice, then the full gate:

```bash
rtk python3.12 -m pytest -q
```

## Definition of done

- The selected upstream installation, not the entire Home Assistant instance, is
  the discovery boundary.
- Every supported battery/system has researched primary integration coverage and
  a tested adapter or explicit manual-only behavior.
- Existing normalized entities and consumers remain compatible.
- Recommended rich metrics appear automatically and are correctly grouped.
- Users can expand, hide, or explicitly include optional metrics.
- Disabled, stale, unavailable and ambiguous sources are honest and fail safe.
- No extra recorder duplication or generic write/control exposure is introduced.
- Config-entry, YAML legacy, duplicate-rate and multi-device edge cases pass.
- The dashboard, API, diagnostics and connection-profile lifecycle agree on the
  same source identity and runtime state.
