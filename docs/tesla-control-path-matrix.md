# Tesla control path review and verification matrix

This checklist covers the Home Assistant integration. It inventories Tesla
battery mutations, their admission and restoration obligations, focused
software coverage, and the local Home Assistant scenarios still requiring
verification. A route decision, submitted callback, API acknowledgement,
settings readback and measured battery response are separate evidence.

**Reviewed 31 October 2026.** All 46 rows have software evidence and an explicit
local-site assessment in [the validation report](tesla-control-validation-2026-10-31.md).
The audited final source run passed **9,408 tests, zero skipped**. The local site
runs unpublished candidate `2.12.1573.dev3` from source `ac766794`; archive and
installation proof are recorded in the report. Live receipts distinguish
acknowledgement, settings readback and measured power. Unavailable hardware,
unsupported capabilities and unsafe fault variants remain identified; the
matrix does not claim universal physical verification.

## Review contract

- Review every production write owner below before release. A new or moved
  energy-site POST must update the inventory test and this checklist.
- Use the physical Tesla energy-site identity across providers and entries.
  Serialize only mutation submission; telemetry reads and control decisions
  must remain responsive while a provider response is delayed.
- Keep each existing admission fence: runtime generation, paired identity,
  startup/restore ownership, signed-settings quarantine, monitoring, demand
  protection, Max Backup, network export protection, licensing and EV budgets.
  A typed route plan never grants write authority.
- Recheck the caller's final authority after waiting for the site gate. A
  check immediately before `session.post()` is now before an additional lock
  wait. Thread the current predicate, returned synchronous write fence and
  original deadline to the final gate; merely importing a controlled session
  does not preserve a caller's stale-write protection.
- Preserve the original request deadline and captured restore baseline.
  Persist intent before an actuator can receive a write. Cancellation or a
  missing reply cannot prove that a signed command was unsent.
- A supervised cloud handoff leaves the unresolved local signed-write incident
  in place. It does not grant new local-write authority or establish that an old
  signed command expired without effect.
- Treat explicit battery watts, grid-export watts, site-import watts and native
  Max as different targets. Positive numeric requests must retain local
  precision under Tariff, Local V1R and Automatic preferences.
- Run the focused package during development, then the current mandatory
  regression release gate. Source inventory assertions complement behavioral
  tests; they cannot establish device acceptance or hardware behavior.

## Production mutation inventory

`tests/test_tesla_control_path_matrix.py` records exact energy-site POST owners
and counts. The shared HTTP session wraps each energy-site mutation at its
submission boundary, including retries. Local V1R classifies the envelope:
only proven reads bypass its site mutation gate; unknown envelopes are treated
as mutations. Existing settings journals and provider fences remain responsible
for whether a write may occur.

For HTTP callers the gate accepts internal `_power_sync_is_current`,
`_power_sync_before_send`, `_power_sync_on_rejected` and
`_power_sync_deadline` markers. The wrapper removes them before creating a raw
request. Only a gate/fence/deadline rejection before invoking the raw driver
can establish `not_submitted`; driver errors and cancellation after submission
must retain the uncertain outcome. The actual tariff-writer queue regression
exercises both its current predicate and returned write fence across this wait.

| Entry or owner | Mutation or downstream sink | Required common gate and existing proof |
| --- | --- | --- |
| `handle_force_charge`, `handle_force_discharge`, `handle_restore_normal` | Native Max adapter, local dispatch, or cloud forcing settings/tariff | Site coordinator plus original route, settings and restore fences; durable root baseline and original deadline |
| `handle_hold_battery_soc`, `handle_set_self_consumption`, their expiry/restore callbacks | Reserve/mode helpers and canonical restore service | Same site gate; saved reserve provenance, override generation and exact previous mode; repeated idle-off cannot upload a historical tariff |
| `_send_tariff_to_tesla` | `time_of_use_settings` POST, including retry | Controlled HTTP session; tariff reservation, per-attempt before-write fence and direct tariff readback |
| `_tesla_force_set_operation_mode.post` | `operation` POST | Controlled HTTP session; current command generation, settings ownership and confirmed mode |
| `_tesla_force_apply_export_rule.cloud_request` | `grid_import_export` POST | Controlled HTTP session; export-only dispatch, submission status and required preference readback |
| `_tesla_force_set_backup_reserve_cloud` | `backup` POST | Controlled HTTP session; reserve lock, command/reserve/kick generation fences and reserve readback |
| `async_set_tesla_grid_charging_confirmed` | Initial and bounded same-payload resend to `grid_import_export` | Every caller supplies a controlled session; original confirmation/operation deadline and current predicate |
| `async_restore_tesla_grid_charging_confirmed` | Restrictive cloud recovery through the grid helper | Controlled session; matching gateway DIN, current restore owner and explicit boolean readback before release |
| `TeslaEnergyCoordinator.set_grid_charging_enabled_outcome` | Grid helper through its coordinator session | Controlled session at call or session binding; invalidate cached site info only after an applied result |
| `TeslaEnergyCoordinator._tesla_api_call` and `async_set_storm_watch`, `async_set_off_grid_ev_reserve`, `async_set_vpp_enrollment` | Generic `session.request("POST", ...)` to storm mode, off-grid vehicle reserve and programs | Controlled session must intercept generic `request`, not just `.post`; GET capability/status probes remain read-only |
| `handle_set_autonomous`, `handle_set_operation_mode._cloud._post_mode` | `operation` POST | Controlled HTTP session plus entry selection, local-settings quarantine and manual command fences |
| `handle_set_backup_reserve`, `handle_set_grid_charging`, `handle_set_grid_export` | Canonical local/settings or cloud helpers | Entry-scoped service, field-specific setting dispatch, existing journal and direct readback |
| `handle_set_grid_export_auto` | Clear manual export preference ownership | Entry-scoped state/persistence update; no invented battery actuator write or unrelated EV lease release |
| `_tesla_charge_kick._mode_bounce` | Two `operation` POSTs | Controlled HTTP session plus original kick/command generation; failed bounce restores without repeating stale forcing |
| `_handle_sync_tou_internal` | Autonomous mode bounce and tariff sync | Controlled HTTP session; existing force/idle/monitoring/ownership deferrals |
| `_handle_sync_tou_internal._calibration_recovery_check` | Recovery `operation` POST | Controlled HTTP session plus calibration and current-entry fences |
| `_handle_sync_tou_internal._retry_autonomous_restore` | Delayed `operation` POST | Controlled HTTP session plus original operation/command ownership |
| `AEMOSpikeManager._enter_spike_mode`, `_exit_spike_mode` | Mode writes and tariff upload/restore | Controlled HTTP session; event ownership, saved real tariff and durable restoration before event clear |
| `SavingSessionTariffManager._enter_session_mode`, `_exit_session_mode` | Mode writes and tariff upload/restore | Same site gate; original session tariff and retained event state when restoration fails |
| `handle_solar_curtailment_check`, `handle_solar_curtailment_with_websocket_data` | Curtail/resume `grid_import_export` POSTs | Controlled HTTP session; existing manual export, EV override, monitoring and curtailment admission |
| `PowerSyncOptionsFlow._restore_export_rule` | Restore `battery_ok` when disabling solar curtailment | Controlled HTTP session; selected options-flow site; no raw POST bypass |
| `AutoScheduleExecutor._set_export_rule` | `set_grid_export` service with `entry_id` and response | Canonical field-specific service; exact success/acknowledgement contract; no parallel direct reserve writer |
| `PowerwallLocalClient` construction in `views.py` | All paired local mutations | Factory binds site coordinator, current entry generation, current identity and controlled cloud-session factory |
| `TEDAPIv1rTransport.post_v1r` | Signed file/config writes, timed battery dispatch, resume and island requests | Classify envelope, then site `run_write`; prepare/persist before submission; retain signed expiry and immediate caller fence |
| `NativeMaxChargeAdapter`, `LocalBatteryDispatch` | Transport config, battery dispatch and resume sinks | The same transport gate, preserving their independent ownership journals, per-write fences, site/EV limits and readback |
| `PowerwallLocalClient._send_signed_device_command` | Signed island request relayed through cloud `device_command` | Factory-bound controlled cloud session and current identity; no unbound mutation fallback |
| Local service/view island/reconnect, calibration-delay and on-grid-solar handlers | Client signed config/TEG/cloud relay paths | Same factory-bound transport/cloud gates; service-specific admission, readback and durable calibration request journal retained |
| `PowerwallCloudProbeView.post` | Authenticated arbitrary energy-site POST | Controlled session; diagnostic provenance still creates a real mutation receipt |
| `PowerwallDebugProbeView.post` | Authenticated legacy local REST POST | Site `write_context`; ordinary GET and local login are separate from the settings mutation |

The following POSTs are explicit **read-only or provisioning classifications**,
not inferred battery-control success. BMS/solar-string query envelopes and
`fetch_device_controller_json` are read requests even when Tesla transports them
over `device_command` or V1R. `_verify_pairing_cloud` lists authorized clients.
Pairing registration/verification, Hermes authentication and the debug-probe
login establish access rather than changing battery dispatch or site settings.
They retain their existing authenticated workflows. An unknown signed envelope
must not inherit these read exemptions.

## Software scenario matrix

`test_tesla_control_path_matrix.py` executes the actual typed targets, route
planner, numeric watt calculator, runtime admission helper and extracted local
force entry paths. It also audits production POST owners, controlled session
construction, the grid helper's indirect coordinator caller, canonical EV
services and final signed-write gates. The suites below supply the deeper
lifecycle and provider fault coverage; run them together after integration.

| Scenario IDs | Required software behavior | Actual focused coverage |
| --- | --- | --- |
| T01–T03 | Cloud-only Tariff charge/discharge reach cloud without a LAN poll; restore retains the original tariff/settings and ownership until confirmed | `test_powerwall_control_method.py`: `test_tariff_control_reaches_cloud_without_reading_unreachable_gateway`, `test_tariff_waits_for_durable_native_ownership_before_falling_through`; `test_tesla_idle_restore.py`; `test_pending_tesla_restore.py` |
| T04–T06 | Every preference preserves explicit numeric battery/export/site-import semantics; spread charging accounts for site load, solar and outstanding EV capacity | New target/entry-path matrix; `test_powerwall_battery_dispatch.py`: `test_export_target_accounts_for_home_and_solar`, `test_native_ev_reservation_reduces_grid_following_battery_without_rewriting_limits`; `test_powerwall_full_rate_charge.py` |
| T07–T09 | Local Max never enables tariff fallback; Auto selects by complete inverter envelope, fails conservatively and retains a running native episode | New Auto envelope matrix; `test_powerwall_control_method.py`: `test_local_preference_never_enables_a_tariff_fallback`, `test_auto_keeps_a_native_episode_stable_until_it_finishes`; `test_powerwall_native_max_adapter.py` |
| T10–T12 | Hold SOC/self-consumption retain correct reserve/mode provenance; expiry restores only owned settings; auto export clears only the manual preference | `test_tesla_idle_restore.py`; `test_tesla_reserve_provenance.py`; `test_tesla_grid_export_service.py`; `test_tesla_local_settings.py` |
| M01–M06 | Mode, reserve, grid charging/export, on-grid solar and Max Backup use the selected entry and existing readback; restoring one field cannot overwrite another owner's settings | `test_tesla_local_settings.py`; `test_tesla_settings_supersession.py`; `test_tesla_grid_export_service.py`; `test_powerwall_on_grid_solar_service.py`; `test_tesla_grid_mode_switches.py`; `test_schedule_max_backup_reserve.py` |
| M07–M11 | Cloud storm/off-grid EV reserve/VPP generic requests use the gate; island/reconnect and calibration-delay mutations retain their own admission and signed deadlines | New generic HTTP request/production delegate matrix; `test_tesla_grid_mode_switches.py`; `test_powerwall_calibration_delay_services.py`; `test_powerwall_calibration_delay_views.py` |
| E01–E03 | Two VIN leases share one original export rule; only final release restores it; restart, failed storage, cancellation and acknowledgement loss retain recovery | `test_ev_powerwall_curtailment_recovery.py`: `test_two_vehicle_leases_release_only_the_last_owner`, `test_restarted_vehicle_reacquires_while_other_recovered_lease_releases`, `test_restart_recovers_original_and_restores_without_vehicle_configuration`, persistence/cancellation cases |
| B01–B04 | TOU sync, spike/session transitions, curtail/resume and charge kicks share the site gate and retain their earlier admission/restore behavior | Production inventory assertions; `test_static_tesla_tariff_sync.py`; `test_tesla_spike_session_tariff_baseline.py`; `test_curtailment_restore_manual_override.py`; `test_tesla_charge_kick_runtime.py` |
| S01–S05 | Preference cannot override monitoring, demand/network protection, permissions, stale/invalid/off-grid/SOC evidence or shared EV/site capacity | New native-admission/invalid-physics matrix; `test_powerwall_control_method.py`; `test_powerwall_grid_permission_encoding.py`; `test_powerwall_native_max_adapter.py`; `test_powerwall_battery_dispatch.py` |
| S06 | Numeric/string site IDs and different providers/entries serialize one physical site's mutations; different sites remain independent | `test_powerwall_control_coordinator.py`; controlled HTTP/session tests; source factory/session assertions |
| R01–R03 | Uncertain local writes remain quarantined; supervised cloud resumption leaves local suspended; startup restores rather than replaying a force request | New native-admission matrix; `test_tesla_local_settings.py`: lost-response, cloud-handoff and interrupted-preparation cases; `test_powerwall_native_max_charge.py`: restart/restore cases; `test_pending_tesla_restore.py` |
| R04–R07 | Queued cancellation, unload, deadline expiry and identity/generation changes cannot later submit a stale write or clear newer ownership | `test_powerwall_control_coordinator.py`; `test_tesla_local_settings.py`: queued cancellation, queue deadline, unload drain and supersession cases; `test_powerwall_native_max_charge.py`: expiry/shortening/cancel cases |
| R08 | A submitted response may remain pending while status reads advance; an unrelated successful operation cannot clear its uncertain effect or durable journal | `test_powerwall_control_coordinator.py`: cancelled direct I/O, unknown receipt, response-error and status responsiveness cases |
| D01–D02 | Every mutation owner uses a shared gate; only actionable persistent native blockers notify; normal SOC/EV/monitoring/restore deferrals remain quiet | New source inventory; `test_powerwall_native_max_notifications.py`; `test_powerwall_native_max_adapter.py`: precise structured policy and final-authority propagation |

`test_powerwall_control_mutation_gates.py` additionally exercises real local
transport classification, complete config read-modify-write transactions,
signed expiry, delayed/cancelled I/O, current factory binding and HTTP marker
stripping/unsent receipts. Those tests validate the gate implementation; the
entry-path matrix validates its callers and physical-target contracts.
`test_tesla_background_control_currency.py` covers event queue revocation,
force/native/recovery ownership, session replacement, retained startup cleanup,
and restrictive restoration while stopped or under an active network guard.

## Required local Home Assistant verification

Run only scenarios explicitly authorized for the test site. Use a simulator or
controlled fault injection for unsafe failure conditions; do not create a lost
signed command, grid outage, overload or storage failure on customer hardware
to complete a checkbox. Pure software failure coverage remains valid evidence
at its own level. Record an unavailable or unsafe live variant as unverified.

For each completed row record the exact installed version/archive, HA restart
health, entry/site identity without credentials, UTC interval, requested target,
admission result, coordinator request/effect IDs, provider acknowledgement,
direct settings readback and balanced fresh battery/grid/load/solar samples.
Include the original baseline and the final restored values. `submitted`, a
returned callback, a service success flag or an HA dashboard state alone cannot
mark the physical-effect column passed.

The table below defines each row's acceptance criteria. IDs link to the
validation report's completed review, which records the evidence level and
remaining variants. A reviewed row is not a claim that every physical variant
was exercised. Expand each method, direction and provider variant into its own
receipt when the test installation supports it.

| ID | Local HA scenario | Required observation before marking complete |
| --- | --- | --- |
| T01 | Tariff charge, manual and optimizer sources; cloud-only/unreachable-LAN variants | No LAN prerequisite, confirmed cloud preparation/tariff, expected charging under site limits; original deadline unchanged |
| T02 | Tariff discharge, manual and optimizer sources; cloud-only/unreachable-LAN variants | Correct export permission and discharge/reserve behavior, measured site export within effective limits |
| T03 | Stop/expiry/restore after T01 and T02 | Owned settings and exact original tariff restored using saved user-reserve provenance; retained owner until required readback/settling. Record intentional untracked Tariff export policy separately |
| T04 | Numeric battery charging under Tariff, Local V1R and Automatic | Same explicit battery-watt request stays local; battery input matches requested semantics within headroom |
| T05 | Numeric grid-export target under all three preferences | Local battery target accounts for home load and solar; measured grid export follows requested quantity |
| T06 | Site-import/spread target under all preferences, with zero/one/two EV reservations | Requested site input remains distinct from battery input; outstanding EV allocation is subtracted without rewriting configured limits |
| T07 | Local V1R Max charge/discharge, including local refusal | Successful local behavior or precise refusal; no silent tariff fallback after a failed local route |
| T08 | Automatic charge/discharge on both sides of the full inverter-envelope boundary | Correct local/native selection and physical behavior for each route; unknown/stale evidence cannot authorize local Max |
| T09 | Telemetry/load changes during an active Automatic native episode | Stable owned episode and original end time; restoration before a replacement route starts |
| T10 | Hold SOC, release and expiry during idle/force transitions | Correct captured reserve, no new force replay and restoration only of the owned override |
| T11 | Self-consumption/idle activation, deactivation and expiry | Correct mode/reserve restoration; repeated idle-off does not upload an unrelated historical force tariff |
| T12 | Return grid export preference to Auto during manual/EV ownership | Only manual preference ownership clears; the EV lease and actual export permission remain independently accounted for |
| M01 | Manual operation mode changes, paired/unpaired cloud fallback | Selected entry only; exact requested mode readback and no stale bounce overriding it |
| M02 | Manual backup reserve changes during idle and after a force handoff | Correct reserve readback and no overwritten baseline/newer owner |
| M03 | Grid charging enable/disable and supported absent-field response | Correct boolean permission/readback contract; unknown permission never becomes a new enable |
| M04 | Grid export `never`, `pv_only`, `battery_ok`; absent/invalid preference handling | Field-specific write and verified result; no reserve/mode mutation caused by an export-only EV request |
| M05 | Explicit on-grid solar-curtailment setting for the selected paired entry | Fresh native readback and intended measured curtail/resume; unrelated entry unaffected |
| M06 | Max Backup entry, exit and overlap refusal | Existing backup commitment remains authoritative; conflicting charge/discharge denied and baseline later restored |
| M07 | Authorized Storm Watch change and direct status refresh | Generic cloud request joins the site gate, selected site only, direct enabled/active state verified |
| M08 | Authorized off-grid vehicle reserve change | Generic cloud request joins the site gate; exact reserve readback independent of main battery backup reserve |
| M09 | VPP enrollment API in an explicitly authorized test environment | Generic program mutation joins the site gate; account/program effect separately confirmed; otherwise record this row unverified |
| M10 | Explicit authorized island/reconnect via service, switch and local view | All entry paths share bound site/identity control and terminal grid-state readback; run in a simulator if physical islanding is not authorized |
| M11 | Explicit calibration delay via service/API plus read-only status | Durable request ID and signed acceptance window survive reload; no repeated delayed request after a crash; existing calibration guard retained |
| E01 | Two-VIN Smart Schedule acquisition and staggered release | One captured original export rule; first release leaves second lease active; final release restores the original |
| E02 | Restart with saved EV leases and one VIN reacquiring | Recovered release intent persists; reacquiring VIN retains ownership; no duplicate baseline capture |
| E03 | EV export write/recovery acknowledgement failure in a simulator | No false acquisition/success; original recovery survives failure/cancel/backoff without repeated reserve writes |
| B01 | Scheduled/manual TOU sync during idle, force, monitoring and restore | Existing deferrals remain; allowed upload is gated/read back and cannot replace an owned force baseline |
| B02 | AEMO spike and saving-session enter/exit/retry | Correct scoped mode/tariff transition; failed exit retains event and original baseline for recovery |
| B03 | Poll and websocket solar-curtailment start/resume; EV/manual override | Same site serialization, expected admission and measured solar/export behavior for both sources |
| B04 | Charge kick/bounce with completion, new command and cancellation | No stale mode/reserve replay; actual restore/charge result tracked independently of a queued kick |
| S01 | Monitoring/external-control activation while a request waits | No new PowerSync actuator write after authority is lost; intentional read/diagnostic work remains available |
| S02 | Demand-charge and network-export protection; Max Backup overlap | Direction-specific denials preserved with accurate reason and no routine alert spam |
| S03 | Disabled/unknown grid charge and export permissions | Precise refusal; no inferred permission from a malformed/missing value; original configuration preserved |
| S04 | Stale/invalid readings, off-grid and SOC floor/cap variants in a simulator | Denial before mutation; no preference turns missing evidence into control authority |
| S05 | Managed/native EV activity and pending capacity during charge/discharge | Shared connection budget remains enforced and no contradictory native ownership is acquired |
| S06 | Same Tesla site through two entries/providers plus a separate site | One mutation sequence for the shared site, independent progress for the other site, identity changes fenced |
| R01 | Existing unresolved local signed-settings incident | Conflicting local/cloud mutation remains blocked; original incident and restoration basis retained |
| R02 | Explicit supervised cloud resumption of R01, then restart | Allowed cloud workflow retains the incident; local signed writes remain suspended across restart |
| R03 | Restart during active/local/native/interrupted preparation | Cleanup restores captured settings, never replays the prior force action, and gates optimizer startup until verified |
| R04 | Reload/unload with queued and submitted work | Queued work cannot later send; submitted uncertainty and terminal persistence remain owned and observable |
| R05 | Queue/read/provider wait crossing the original deadline | No late force or deadline extension; cleanup remains possible after forcing authority expires |
| R06 | Cancellation/response loss after submission in a simulator | Unknown effect retained; a returned callback is not reported as confirmed device state |
| R07 | Entry/site/paired-key/generation replacement during await | Old callbacks cannot write, restore over a newer owner, or clear its UI/journal |
| R08 | Status reads and stop/restore decision while provider reply is delayed | Status/telemetry remains responsive; one unresolved effect cannot be erased by unrelated success |
| D01 | Read-only diagnostic GET/DCQ/BMS and authorized diagnostic mutations | Reads remain responsive; authorized mutation joins the same site gate; query POST is not mislabeled as a battery write |
| D02 | Repeated actionable signed-settings denial, then successful admission | Accurate notice once per continuing blocker; plain actionable recovery reference; clear only after success; SOC/EV/monitoring waits quiet |

## Suggested focused software package

Run from the current clean worktree using the repository's Python 3.12 test
environment. Keep controlled HTTP/local transport tests in the package; do not
replace them with this static inventory.

```sh
/root/codex-workspaces/power-sync-test-venv/bin/python -m pytest \
  tests/test_tesla_control_path_matrix.py \
  tests/test_tesla_force_response_contract.py \
  tests/test_tesla_restore_retry_semantics.py \
  tests/test_powerwall_control_diagnostics.py \
  tests/test_powerwall_control_coordinator.py \
  tests/test_powerwall_control_mutation_gates.py \
  tests/test_powerwall_control_method.py \
  tests/test_powerwall_battery_dispatch.py \
  tests/test_powerwall_native_max_adapter.py \
  tests/test_powerwall_native_max_charge.py \
  tests/test_powerwall_native_max_notifications.py \
  tests/test_powerwall_grid_permission_encoding.py \
  tests/test_powerwall_full_rate_charge.py \
  tests/test_tesla_local_settings.py \
  tests/test_tesla_settings_supersession.py \
  tests/test_pending_tesla_restore.py \
  tests/test_tesla_grid_control.py \
  tests/test_tesla_grid_retry_runtime.py \
  tests/test_ev_powerwall_curtailment_recovery.py \
  tests/test_tesla_charge_kick_runtime.py \
  tests/test_tesla_idle_restore.py \
  tests/test_tesla_spike_session_tariff_baseline.py \
  tests/test_tesla_background_control_currency.py \
  tests/test_static_tesla_tariff_sync.py \
  tests/test_powerwall_on_grid_solar_service.py \
  tests/test_schedule_max_backup_reserve.py \
  tests/test_powerwall_calibration_delay_services.py \
  tests/test_powerwall_calibration_delay_views.py \
  tests/test_curtailment_restore_manual_override.py -q
```

Publication still requires the current tracked release procedure and trusted
matching full-suite evidence. Successful publication is separate from
installation and local live verification of the rows above.
