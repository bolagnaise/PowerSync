# Vehicle-to-Home Planning and Control Architecture

Status: architecture proposal

Evidence date: 2026-08-15

Runtime status: not implemented; no V2H commands or user-facing settings

## Decision

PowerSync should model flexible vehicle-to-home energy as a separate,
provider-neutral, finite energy resource. It must not be represented as a
negative load, stationary-battery discharge, or proof that PowerSync can
control a bidirectional charger.

The work should proceed in four deliberately separate layers:

1. Observe signed EV power and keep canonical non-EV Home Load correct.
2. Plan a session-scoped external energy allowance without hardware writes.
3. Co-optimize a verified flexible resource with explicit source-to-sink
   constraints.
4. Execute only through a provider adapter that proves capability, ownership,
   command acceptance, measured transfer, and safe restoration.

The first internal implementation slice should be planning-only and
import-offset-only. It may reduce forecast native-home grid imports. It must
not increase grid export, stationary-battery charging, grid import, or planned
EV charging. It must not acquire an EV ownership lease or call a provider.

This is a code-confirmed feature gap, not a defect in the current optimizer.

## Ticket problem

Ticket #381 describes an EV that can contribute roughly 10 kWh after solar
production ends. The user may use that energy to cover house load or replenish
the stationary battery. The current optimizer does not know the source exists,
so it can forecast that the stationary battery will empty around 04:00 and
plan grid import even though the user's external V2H arrangement will cover
the load.

The request sounds like one numeric setting, but a bare 10 kWh field has no
safe rolling-horizon meaning. It does not establish:

- when the source is available;
- the maximum power it can deliver;
- whether 10 kWh is vehicle capacity, usable DC energy, or net AC energy;
- whether that energy has already been used during the active session;
- whether the source follows home load, charges the stationary battery, or
  exports to grid; or
- whether PowerSync can command it or merely assumes another controller will.

The architecture below gives the energy field an exact meaning and supplies
the missing boundaries.

## Evidence boundaries

The following are different facts and must remain different in code and UI:

- configured capability;
- product-advertised capability;
- vehicle/EVSE/inverter eligibility;
- current negotiated capability;
- planning assumption;
- command requested;
- command accepted;
- hardware readback;
- measured signed power; and
- sustained delivered energy.

A product page, bidirectional protocol, signed power sample, accepted charging
command, or active process does not prove controllable V2H dispatch.

Australian Government terminology also requires compatible bidirectional
equipment and vehicle-maker warranty/service support before treating a vehicle
as verified for V2H or V2G. Grid export additionally depends on site and
network approval.

## Current PowerSync seam map

| Concern | Current seam | Existing behavior | V2H boundary |
|---|---|---|---|
| EV observation and Home Load | custom_components/power_sync/ev_load.py | EvLoadObservation accepts negative power only when supports_bidirectional_power is true. normalize_home_load subtracts signed EV power once. | Keep observation and attribution here. Do not add inventory, optimization, or commands. |
| Sigenergy signed power | sigenergy_model.py and sigenergy_charger.py | EVDC discharging is normalized to negative kW and projected into loadpoint status. The controller supports charging start/stop, not verified V2H dispatch. | Observer only until a real discharge command contract is proven. |
| Planned EV demand | OptimizationCoordinator._get_planned_ev_load_forecast | Reads positive forecast-only charging demand and adds it to the load forecast. | Do not encode V2H as negative planned EV demand. Preserve EV charging as a separate sink. |
| Battery planning | BatteryOptimizer.optimize, _solve_lp_inner, and _solve_greedy | Plans grid import/export and one stationary battery. The LP uses an aggregate site power balance. | Accept resolved external sessions separately. Preserve an empty/default path with exact no-resource parity. |
| Runtime assembly and API | OptimizationCoordinator._run_optimization and get_api_data | Resolves HA inputs, invokes the optimizer, and reports battery/grid results. | Resolve resource sessions and ledger before the solver. Own provider-neutral result diagnostics. |
| EV charging control | optimization/ev_coordinator.py | Plans unidirectional charging and uses the shared EV action layer. | Observation may feed a V2H resource; planning must not call its start/stop/current paths. |
| Loadpoint ownership | automations/ev_ownership.py | can_claim_ev_ownership, claim_ev_ownership, and release_ev_ownership arbitrate commands. | Future execution gate only. Planning and observation never acquire ownership. |

### Load sign invariant

PowerSync's canonical sign convention is:

- EV charging: positive demand;
- V2H discharge: negative EV power; and
- canonical non-EV Home Load:

~~~text
non_ev_home_load = raw_home_load_including_ev - signed_ev_power
~~~

Subtracting a negative V2H value correctly adds the vehicle-supplied power back
to canonical non-EV demand. That prevents an actively discharging EV from
making the underlying house load look artificially low.

Observed V2H must therefore appear exactly once:

1. signed EV power is removed from gross Home Load attribution; and
2. delivered V2H energy is recorded separately against the active external
   resource session.

It must not also be inserted as negative forecast load.

Before a provider observer relies on fallback meters,
reconcile_ev_load_snapshot must be reviewed: its direct fallback values
currently pass through normalization without bidirectional capability
metadata, so a negative fallback can be rejected even though the primary
observation contract supports it. This is a precondition for a later observer
slice, not evidence that current primary Sigenergy telemetry is wrong.

## Three planning cases

### Observed only

PowerSync can see signed transfer but cannot assume future availability or
choose dispatch. Observation fixes Home Load, updates a session ledger when
one exists, and appears in diagnostics. It does not change the forecast by
itself.

### Exogenous forecast

Another controller or the user supplies a time-indexed V2H power plan.
PowerSync treats that as external supply forecast, not a decision variable.
The source must be timestamped, freshness-checked, and explicitly labelled as
an assumption. A total kWh value alone cannot define this case because it says
nothing about timing.

### Flexible finite resource

PowerSync is allowed to choose when a bounded quantity may support the home.
This needs an availability session, remaining net AC energy, maximum power,
and sink policy. It becomes an optimizer decision but remains planning-only
until a controller adapter separately proves dispatch capability.

These cases share diagnostics and identity, but they must not share authority.

## Resource contract

The optimizer should receive fully resolved, immutable sessions. It should not
read Home Assistant state, config entries, storage, or provider APIs.

An eventual provider-neutral type should contain at least:

~~~python
ResolvedExternalEnergySession(
    resource_id: str,
    session_id: str,
    loadpoint_id: str | None,
    planning_mode: str,
    control_capability: str,
    sink_mode: str,
    remaining_ac_kwh: float,
    available_slots: tuple[bool, ...],
    max_discharge_kw: tuple[float, ...],
    observation_quality: str,
    source_updated_at: datetime | None,
)
~~~

The planned result should remain separate from ScheduleAction so the existing
stationary-battery executor cannot mistake resource discharge for a battery
force-discharge command:

~~~python
ExternalEnergyPlan(
    resource_id: str,
    session_id: str,
    planned_discharge_w: tuple[float, ...],
    planned_energy_kwh: float,
    remaining_after_plan_kwh: float,
    planning_mode: str,
    control_capability: str,
    reason: str | None,
)
~~~

The API/result contract should default to:

~~~json
{
  "external_energy_resources": []
}
~~~

That default is part of backward compatibility. No existing entry should gain
an assumed resource.

## Exact meaning of the kWh value

The future user setting should mean:

> Maximum net AC energy PowerSync may assume can be delivered at the home
> connection during one identified availability session, after vehicle reserve
> and conversion losses.

It is not total EV capacity, raw DC energy, a recurring daily credit without a
schedule, or proof that the energy is still present.

For a generic planning assumption, the minimum configuration is:

- source/loadpoint binding, or an explicit generic source;
- usable AC kWh per session;
- maximum discharge kW;
- availability start and end;
- enabled state.

The first exposed mode should be fixed internally to import_offset_only.
Stationary-battery charging, islanding, and grid export must not be selectable
until their separate models and capability gates exist.

If a provider later supplies fresh SOC, capacity, reserve, conversion, plug
state, departure time, and power limits, the UI may derive most fields. In that
case the user's only additional choice can be a maximum usable energy
allowance. The resolved optimizer contract does not change.

## Session and rolling-solve semantics

Every occurrence of an availability window is a separate session:

- A 22:00–06:00 session is keyed by its 22:00 start instant, not the local date
  observed at 03:00.
- The configured budget is granted once per explicit session.
- Re-running the optimizer never replenishes the active session.
- Unused energy does not carry into another session.
- A recurring future window gets a new budget only because recurrence is
  explicitly configured.
- A 48-hour horizon may contain several sessions; each gets an independent
  inventory chain.
- Multiple resources never share one global budget.

The active-session ledger key should be:

~~~text
config_entry_id + resource_id + session_start_utc
~~~

For each elapsed slot:

1. Use integrated, validated measured V2H energy when available.
2. Otherwise conservatively count the previously planned elapsed energy as
   consumed.
3. Never count measured and assumed energy for the same slot.
4. Never lower consumed energy because delayed telemetry later reports less
   than the conservative fallback.
5. Discard and freely replan only future allocations.

On restart, restore the active ledger. If persistence or valid recorder
history cannot reconstruct it, resolve the active session to zero remaining
energy. Never restore a full budget. Future sessions may still be generated
from their explicit schedule.

This ledger makes a static per-session allowance honest across rolling solves.

## Optimizer formulation

For resource r, base slot t, and slot duration dt in hours:

~~~text
0 <= external_power[r,t]
   <= available[r,t] * max_discharge_power[r,t]

energy[r,t+1]
   = energy[r,t] - external_power[r,t] * dt

0 <= energy[r,t]
energy[r,session_start] = remaining_ac_kwh
~~~

Availability boundaries, session boundaries, power-limit changes, and
same-vehicle charging changes are correctness-sensitive inputs. Internal LP
period aggregation must split when any of them changes.

An EV-bound resource also needs:

~~~text
planned_ev_charge[t] > 0  =>  external_power[r,t] = 0
~~~

The vehicle must never be planned to charge and discharge in the same slot.
A later fully bidirectional model should use mutually exclusive charge and
discharge modes and retain departure energy/SOC targets.

### Initial import-offset-only slice

The current LP has one aggregate bus balance rather than complete
source-to-sink flow attribution. Adding unrestricted external supply would let
the optimizer use a nominally home-only EV to charge the stationary battery,
free stationary energy for export, or support planned EV charging.

The first code slice should therefore use a constrained second-stage allocator:

1. Run the existing optimizer unchanged to obtain the stationary-battery
   schedule and grid flows.
2. Derive eligible native-home import from the same schedule using canonical
   non-EV load, excluding planned EV charging and stationary-battery charging.
3. Allocate each external session only against positive eligible import,
   subject to availability, slot power, and remaining energy.
4. Prefer the slots with the highest avoided import value; use chronological
   order to break equal-price ties.
5. Keep every stationary-battery action unchanged.
6. Subtract only allocated external power from forecast grid import.
7. Leave grid export unchanged.
8. Recompute predicted cost and external-resource diagnostics from the emitted
   plan.

This is sufficient for the first mode because the only allowed value is
avoided native-home import. It is also structurally incapable of charging the
stationary battery or exporting the external energy.

The same pure allocator should run after both HiGHS and greedy base schedules.
The fallback must never silently ignore a configured resource.

Required invariants for every slot are:

~~~text
grid_import_with_resource <= grid_import_without_resource
grid_export_with_resource == grid_export_without_resource
battery_charge_with_resource == battery_charge_without_resource
battery_discharge_with_resource == battery_discharge_without_resource
external_power <= eligible_native_home_import
~~~

### Later co-optimized home and stationary-battery support

The ticket's full use case includes deliberately replenishing the stationary
battery. That needs explicit source-to-sink allocation rather than an
unrestricted bus source:

~~~text
external_discharge
  = external_to_home
  + external_to_stationary_battery
  + external_to_grid
~~~

Each arc must be capability- and policy-gated. Home-and-battery mode enables
the first two arcs and fixes external_to_grid to zero. V2G enables the grid arc
only after site approval, export limits, tariff policy, provider control, and
measured PCC enforcement are independently verified.

The full model should include charge/discharge efficiency, vehicle reserve or
departure target, marginal degradation/opportunity value, and terminal value.
It should co-optimize stationary and vehicle energy without allowing circular
grid, battery, or EV flows.

## Observation, planning, and execution contracts

Use separate responsibilities:

- ExternalResourcePlanner: produces resolved sessions and planned results.
- ExternalResourceObserver: reports presence, signed power, energy, SOC,
  negotiated capability, freshness, and reasons.
- ExternalResourceController: applies and verifies a requested discharge.

An observer never becomes a controller because a brand name or negative power
was seen.

Planning-only behavior must never:

- claim or release EV ownership;
- change charge current;
- start or stop charging;
- wake a vehicle;
- enable a bidirectional mode; or
- call a provider write API.

Monitoring mode may observe and plan. It must still short-circuit before
ownership or any write.

A future controller may execute only when it can complete the whole lifecycle:

1. Confirm fresh availability and negotiated bidirectional capability.
2. Confirm vehicle, EVSE/inverter, site, and allowed-mode eligibility.
3. Acquire the existing PowerSync loadpoint ownership lease.
4. Apply a bounded discharge request.
5. Verify acceptance or hardware readback.
6. Verify signed measured AC transfer and sustained state.
7. Stop and restore normal charger/vehicle state on completion or failure.
8. Release ownership.

Failure, stale telemetry, disconnect, ownership loss, or command rejection must
remove the resource from the next solve. A requested or accepted command is
not measured delivery.

## Product research snapshot

### Sigenergy SigenStor EV DC

Sigenergy markets its EV DC module as a 25 kW EV-home bridge with V2H/V2G.
Its current product page also states that V2X is limited by vehicle
capability, official vehicle support/timelines remain subject to later
announcement, and field results can depend on model, year, software, region,
or third-party adapters. Some field cases are explicitly not OEM-recognized
or certified.

PowerSync currently has useful observation foundations:

- signed EVDC charging/discharging power;
- EVDC status, SOC, current, voltage, and energy telemetry; and
- normalized loadpoint status.

Its verified control boundary is charging start/stop. A stored discharge-limit
entity or observed negative power is visibility, not a V2H command contract.
Sigenergy must therefore begin as observed_not_dispatchable.

### Tesla Powershare

Tesla currently documents Cybertruck Powershare Home Backup at up to 11.5 kW
and region-dependent Grid Support. Tesla also states that Powershare is
currently unavailable with Powerwall, an OTA integration is planned, and
co-optimization with Powerwall for Grid Support is not supported at present.

Tesla Fleet Telemetry exposes Powershare hours, instantaneous power, state,
stop reason, and type. The public Fleet API vehicle-command reference does not
list a Powershare start, stop, power-target, or discharge-limit command.
PowerSync can design an observer around that telemetry, but must keep Tesla
observation-only until a supported dispatch interface is documented and
verified.

Tesla's Australian Wall Connector release notes mention a V2H beta. That is
not evidence of generally available Australian Cybertruck Powershare,
Powerwall co-optimization, or a third-party dispatch API. No Australian launch
date should be inferred.

### Wider product patterns

Other current and announced systems reinforce the need for capability and
authority to be projected separately:

- Ford Home Backup Power and the GM Energy V2H Bundle are installed,
  vehicle-specific home-energy systems whose documented behavior centres on
  powering an islanded home during an outage. They do not establish a generic
  third-party, grid-connected dispatch interface.
- Wallbox documents six initial US residential Quasar 2 installations with
  compatible Kia EV9 vehicles. That is useful real-world V2H evidence, but the
  stated pilot scope must not be projected as universal vehicle or regional
  availability.
- Enphase describes an ISO 15118-based platform for charging, home backup, and
  future grid services, targeting volume production in Q4 2026. This is an
  announced integration direction rather than current installed capability.
- CHAdeMO has a long-running certified bidirectional ecosystem and explicitly
  distinguishes V2L, V2H, V2B, and V2G applications. Its history demonstrates
  that protocol support and deployed products are still narrower facts than
  eligibility or controllability of a particular home installation.

The recurring market pattern is a tuple of compatible vehicle, bidirectional
EVSE/inverter, transfer or grid-isolation equipment, firmware, site approval,
and vendor control plane. PowerSync should not infer that tuple from a vehicle
or charger brand alone.

### Standards and Australia

ISO 15118-20 defines communication messages and sequences for bidirectional
power transfer. Protocol support is one capability input; it does not prove
that a particular EV, charger, firmware, account, site, or third-party API can
dispatch V2H.

Australia has a national roadmap for commercial bidirectional charging and
active trials. Current Government definitions distinguish V2H, V2G, and V2L
and require compatible equipment plus vehicle-maker support for verified
V2H/V2G. PowerSync should represent approvals and eligible modes separately
instead of using one is_v2x flag.

## Capability projection

Each resource should eventually project independent, evidence-backed fields:

- connector and protocol;
- vehicle capability and OEM support;
- EVSE/inverter capability;
- current negotiated mode;
- site/grid approval;
- islanding state;
- home-support permission;
- grid-export permission;
- observation capability;
- planning capability;
- command capability; and
- command/readback/measurement freshness.

Suggested high-level execution values are:

- unsupported;
- planning_assumption;
- observed_not_dispatchable;
- dispatchable_unavailable;
- dispatchable_available;
- monitoring;
- controlled;
- degraded.

These are projections of evidence, not provider marketing labels.

## Diagnostics

Expose enough state to distinguish the plan from physical behavior:

- resource ID and loadpoint ID;
- session ID and availability window;
- configured usable AC kWh;
- measured or conservatively assumed used kWh;
- resolved remaining kWh;
- planned power by slot and planned kWh;
- observed signed power and delivered kWh;
- availability source and freshness;
- observation quality;
- planning mode and sink mode;
- control capability;
- ownership state;
- requested, accepted/readback, and measured power separately;
- zero/degraded reason; and
- last successful restore/release.

Do not publish a generic V2H active state while the feature is planning-only.
Use language such as assumed external support, observed V2H transfer, or
controlled V2H transfer.

## Implementation sequence

### Architecture pass

This document is the complete output. It changes no runtime behavior, device,
config entry, release, or ticket state.

### Slice 1: internal import-offset planner

Minimum production files:

- custom_components/power_sync/optimization/external_energy_resource.py
  (new pure types, session expansion, ledger reducer, allocator, diagnostics);
- custom_components/power_sync/optimization/battery_optimizer.py
  (optional empty resource input and result threading); and
- custom_components/power_sync/optimization/coordinator.py
  (empty production collection, eligible native-home input, stable API result).

Minimum tests:

- tests/test_external_energy_resource.py (new);
- tests/test_battery_optimizer_export_guard.py;
- tests/test_optimization_price_source.py; and
- tests/test_run_optimization_atomicity.py.

No options flow, config migration, entity, store, provider adapter, ownership
call, manifest bump, or hidden enabled resource belongs in this slice.

Acceptance criteria:

- no-resource schedule, objective, grid forecast, and control behavior retain
  current parity;
- a 10 kWh, 3.6 kW, 22:00–06:00 synthetic session contributes no more than
  0.3 kWh per five-minute slot and 10 kWh per session;
- cross-midnight and two-session 48-hour horizons are correct;
- multiple resources have independent inventories;
- only eligible native-home grid import is reduced;
- stationary-battery actions and grid export do not change;
- planned EV demand cannot be supplied;
- HiGHS and greedy base schedules share the same resource allocator;
- result units and meanings match;
- invalid input and corrupt active ledger fail closed; and
- ownership/command spies observe zero calls.

### Slice 2: planning-only persistence and configuration

- Persist active-session consumption.
- Add fail-closed restart and recorder-assisted recovery.
- Add optional resource configuration, default disabled.
- Validate partial configs without granting defaults.
- Bind to a loadpoint or generic source.
- Add planning-only HA/API/mobile diagnostics.

### Slice 3: observation adapters

- Carry bidirectional capability metadata through fallback meters.
- Normalize Sigenergy EVDC observations into the provider-neutral observer.
- Add Tesla Fleet Powershare observations only when the public fields are
  available and fresh.
- Reconcile measured delivery with the session ledger.
- Run non-writing replay tests.

### Slice 4: co-optimized home-and-battery support

- Add explicit external-to-home and external-to-stationary-battery arcs.
- Add vehicle reserve/departure, efficiencies, value, and exclusivity.
- Keep external-to-grid disabled.
- Recompute cost, reserve, and resource diagnostics from the emitted plan.

### Slice 5: verified provider execution

- Add one adapter at a time only after a supported discharge command exists.
- Reuse EV ownership.
- Gate monitoring, site mode, firmware, vehicle, and approval.
- Verify requested, accepted, measured, and sustained states.
- Fail safe and restore.

### Slice 6: islanding and V2G

Treat outage backup and grid export as separate products and policies.
Implement neither as a side effect of home support.

## Test matrix for future slices

- no-resource LP and greedy parity;
- energy and per-slot power caps;
- cross-midnight session identity;
- two sessions inside one horizon;
- rolling re-solve consumption;
- restart/corrupt-ledger fail-closed;
- measured-versus-assumed slot reconciliation;
- provider energy/power telemetry may lower but never inflate configuration;
- multiple independent resources;
- same-EV charge/discharge exclusion;
- load attribution counted exactly once;
- no increase in export/import/battery charging for import-offset-only;
- stationary reserve unchanged in the first slice;
- no provider or ownership calls while planning;
- monitoring-mode no writes;
- stale/disconnected/rejected resource removal and re-solve;
- command acceptance does not count as delivery;
- measured and sustained transfer verification; and
- safe stop, restore, and ownership release.

## Open product choices before user-facing configuration

The architecture is ready for an internal slice. A user-facing planning
feature still needs explicit product decisions:

1. Is the source a recurring per-session allowance or a live remaining-energy
   entity?
2. Does another controller guarantee a fixed profile, follow native home
   deficit, or accept a PowerSync dispatch target?
3. Is first public behavior house-load-only, or must deliberate stationary
   battery replenishment ship at the same time?
4. Which provider telemetry can reconstruct an active session after restart?
5. Which EV/EVSE/site combinations have OEM and regional approval?
6. Is grid export ever allowed, and under which network/export-control
   evidence?

Defaults must fail closed; they must not guess these answers from brand names.

## Research sources

- Sigenergy EV DC product and field-test caveats:
  https://www.sigenergy.com/en/products/dc-charger
- Sigenergy Australian EV DC datasheet:
  https://www.sigenergy.com/au/support/files/701
- Tesla Powershare:
  https://www.tesla.com/support/powershare
- Tesla Fleet Telemetry available data:
  https://developer.tesla.com/docs/fleet-api/fleet-telemetry/available-data
- Tesla Fleet API vehicle commands:
  https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-commands
- Tesla Australia Wall Connector release notes:
  https://www.tesla.com/en_au/support/charging/wall-connector/release-notes
- Ford Home Backup Power:
  https://www.ford.com/support/how-tos/electric-vehicles/home-charging/what-is-ford-home-backup-power/
- GM Energy V2H Bundle:
  https://gmenergy.gm.com/vehicle-to-home/gm-energy-v2h-bundle
- Wallbox Quasar 2 initial US installations:
  https://wallbox.com/en_us/blog/quasar-2-bidirectional-ev-charger-menifee-california
- Enphase IQ Bidirectional EV Charger production update:
  https://newsroom.enphase.com/news-releases/news-release-details/enphase-energy-demonstrates-global-iq-bidirectional-ev-charging
- CHAdeMO V2G/VGI:
  https://www.chademo.com/technology/v2g
- ISO 15118-20:
  https://www.iso.org/standard/77845.html
- Australian Government V2X definitions:
  https://www.energy.gov.au/electric-vehicles/electric-vehicle-basics/definitions-electric-vehicles
- ARENA National Roadmap for Bidirectional EV Charging:
  https://arena.gov.au/knowledge-bank/national-roadmap-for-bidirectional-ev-charging-in-australia/
