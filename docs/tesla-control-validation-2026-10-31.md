# Tesla control validation — 31 October 2026

Draft PR: **#138**. Production source: **`ac766794e86fdc1f842786004ec2d9e13e13eeda`**.

The audited software checkpoint passed **9,408 of 9,408 tests**, with zero skips. A development build is installed on the authorized test HA instance. Selected control routes were physically exercised on the earlier dev1 build; the fixes discovered during those trials still require their dev3 live checks. This is not evidence that all 46 scenarios, all providers, or all Powerwall firmware variants have been physically tested. No public release is recorded here.

## Source and candidate provenance

| Item | Recorded evidence |
| --- | --- |
| Candidate | `2.12.1573.dev3`, built from `ac766794e86fdc1f842786004ec2d9e13e13eeda` |
| Archive | `power-sync-2.12.1573.dev3.tar.gz` |
| SHA-256 | `a4e0fb416e9270b910bc8f24d7ea77c0673c0c9898d4113ac2e488669eac18ee` |
| Size / regular files | 43,741,707 bytes / 263 files; locally rechecked against the archive |
| Installation | HA verification owner reports the dev3 archive installed and all 263 installed file digests matching; monitoring remains enabled |
| Config check / restart health | Configuration check `ok`; Core restart `ok`; Docker started 03:03:45 UTC; fresh local API data; zero PowerSync startup errors |
| Publication | `candidate.json` records `published: false` |

Local evidence is retained under `/tmp/powersync-tesla-control-evidence/`. It is an operational evidence directory, not part of the distributed integration. The principal records are `candidate.json`, `candidate-dev1.json`, `matrix-software-evidence.md`, its JSON node expansion, `matrix-audit-reconciliation.json`, `reserve-provenance.json`, `ha-dev1-records-through-expiry.json`, and the later **`ha-dev1-final-records.json`** containing 68 records. The final records add settled expiry and Local Max observations missing from the through-expiry capture. Credentials and full entry/site identities are omitted from this report.

## Control architecture and preserved boundaries

The [control-path matrix](tesla-control-path-matrix.md) inventories the production writers and their admission obligations. The implementation separates the requested quantity, route selection, ownership, mutation submission and observed result:

1. `NativeMaxTarget`, `BatteryPowerTarget`, `GridExportTarget` and `SiteImportTarget` preserve different physical meanings. Explicit positive watt requests remain local under all three saved preferences. Tariff Max does not promise a precise watt target. Automatic evaluates the full inverter envelope and retains an active native episode until cleanup.
2. `PowerwallSiteControl` is shared by the canonical physical energy-site identity across entries and providers. `route_scope(entry_id)` reserves preparation as well as the existing route lock, so a foreign writer cannot slip in before an event or force action publishes its active state. Same-entry cleanup remains possible; different sites retain independent progress.
3. The controlled HTTP session gates energy-site mutations, including generic `request()` and retries. Local signed transport gates mutations and complete configuration read-modify-write transactions; only proven reads bypass this boundary. Current runtime, identity, ownership and original deadline are checked after the queue wait, before invoking the raw driver.
4. Force, background event and EV workflows retain their original restoration owners and durable baselines. Two EV leases share the original export rule; only the final release restores it. A cancelled or lost submitted response remains uncertain until the existing recovery contract resolves it.
5. An unresolved signed-settings incident blocks conflicting writes. Supervised cloud resumption retains that incident and its restore basis, while local signed settings remain suspended. It is not proof that the old local write completed. Monitoring, license, demand/network protection, Max Backup, SOC and EV capacity gates retain their separate authority.
6. Native admission preserves precise structured reasons. Actionable persistent blockers can notify; routine SOC, EV, monitoring, startup and restoration deferrals stay quiet. Diagnostics derive current request/recovery ownership rather than treating a historical receipt as an active control.
7. A callback return, service response, provider acknowledgement, direct setting readback, physical settling and HA presentation are separate outcomes. A manual restore can still be pending after settings change. Startup and optimizer handoffs intentionally retain their safe mode policy; an ordinary manual retry now preserves its original source and force-restore flag.

With Tariff selected, ordinary mode, Grid Charging and Grid Export settings use cloud control without a local-write attempt. Backup reserve and tariff uploads remain cloud operations. Local and Automatic retain their existing guarded local/settings fallback behavior. No preference discards a restoration or signed-write obligation.

## Audited software evidence

The final run used the repository's audited four-worker runner. Its summary is `/tmp/powersync-tesla-control-full-20261031-7/summary.json`:

| Field | Value |
| --- | --- |
| Run ID | `9de7bc745483460b98f031a907bd9c72` |
| Complete / passed / skipped | `true` / `9408` / `0` |
| Workers / mode | `4` / `loadfile` |
| Collection node hash | `3cd386a272dd5653416decf4587fed70a2c64063cbb0b33e4a2a6a66392dfe0b` |
| Elapsed | 413.76 seconds |

The row-to-test mapping names exact node IDs and distinguishes **B** (actual lightweight or extracted production behavior with fake I/O), **L** (pure policy/calculation), and **A** (source/AST wiring assertions). Independent final-run reconciliation validates 46 rows. Of 466 literal associations, 465 match passing nodes; one unparameterized R03 selector became three passing restart cases. Resolving it gives 468 passing associations across 426 unique nodes, with no unresolved references (398 B, 63 L, 7 A associations). Another 66 response-contract, retry-policy, diagnostic and export-basis nodes passed; 63 are additional to the resolved map. Four identical ordered collections and all 28,224 setup/call/teardown phase reports were validated. See `matrix-run7-reconciliation.{json,md}` in the evidence directory.

The full pass establishes software checks on this source. Fake acknowledgements/readbacks cannot establish real device or account behavior. Tests of a final predicate also do not necessarily execute the complete outer service, timer or background workflow. These limits remain visible below.

## Dev1 live observations and fixes found

The saved captures span **02:29:56–03:02:37 UTC** on 31 October. They concern one authorized installation using the configured PowerSync Tesla provider and a paired gateway. Manual requests used `source: user` while monitoring was enabled. Earlier optimizer/validation-source probes include license refusals and response-contract errors; they do not isolate every monitoring gate or prove an optimizer charge/discharge ran.

Power signs below follow the captured local readings: negative battery power is charging; negative grid power is export. Values are samples, not a promise of constant output over the whole interval. Use raw home load and balanced power when checking export semantics: vehicle telemetry and normalized home load can differ from contemporaneous raw site load; these captures do not establish a defect in load normalization.

| Scenario and saved record | Physical/readback evidence | Limit |
| --- | --- | --- |
| Manual Tariff Max charge, `T01-charge-30s`, 02:33:34 | About **11.04 kW battery charging**, **14.75 kW grid import**, with about 3.74 kW home load | One site/provider; optimizer and deliberately unreachable-LAN variants not exercised live |
| Manual Tariff Max discharge, `T02-discharge-stable`, 02:37:27 | About **14.25 kW battery discharge**, **10.10 kW grid export**, about 4.14 kW home load | Initial 30-second sample still showed no battery discharge; acceptance was not immediate physical output |
| Tariff restore captures | Direct tariff checks after charge and discharge matched the original **FOUR4FREE** tariff hash `70238c7bc304c8e74fb5d657413a419838b222efcaecf080dea8f35bf65e8e60` | `matches_original_settings` was false; exact restoration of every initially observed setting is not established |
| Numeric charging under Local, Tariff and Automatic, `T04-*-charge-physical` | 2 kW requests measured approximately **1.998–2.001 kW charging**, with effective route local under all three preferences | These observations predate the response-contract fix; dev3 confirmed-response retest remains pending |
| Numeric export under all three preferences, `T05-*-export-physical` | 1 kW export requests produced approximately **1 kW measured export**, with battery output additionally serving home load | The dev1 target-basis/load presentation defect requires the dev3 diagnostic check |
| Automatic Max charge, `T08-auto-max-charge-physical`, 02:47:40 | Native/tariff route, about **10.98 kW battery charging** and **14.75 kW import** | Does not exercise every inverter-envelope boundary |
| Native-to-numeric handoff, `T09-*` | Replacement first refused with physical settling pending; owner remained. At 02:50:16 the effective episode cleared with about 10 W grid flow, then a later 2 kW request measured about 2.002 kW charging | The initial pending error is intentional safety behavior, not a completed handoff |
| Automatic native discharge and five-minute expiry, `T03-native-*` | About 14 kW battery output and 10 kW export before expiry. At 02:56:41 settings had changed but discharge persisted; at **02:58:16**, battery output served roughly 3.72 kW home load and grid flow was about −2 W. Separate journal capture showed no retained native journal | Physical settling took roughly two minutes after the intended expiry; a changed mode alone did not prove completion |
| Local Max charge, `T07-local-max-charge-physical`, 02:59:10 | About **9.997 kW battery charging**, **13.754 kW import**, about 3.765 kW raw home load | One gateway/firmware combination |
| Local Max discharge, `T07-local-max-discharge-physical`, 03:02:37 | About **9.996 kW battery output**, **6.049 kW export**, about **3.934 kW raw home load** | Local inverter output and grid export are different quantities; this is not 10 kW export |
| Read-only diagnostics during native control, `D01-native-control-reads` | Cloud GET returned HTTP 200 in about 571 ms; gateway read succeeded in about 5 ms; calibration status returned unsupported in about 159 ms | No calibration mutation, islanding, VPP or EV actuation was attempted |

The initial observed hardware reserve was 100%, while both saved data/options configured hardware reserve at **0%** and optimizer reserve at **30%**. `reserve-provenance.json` shows those configured values remained unchanged. Restoring the configured reserve to 0% therefore differs from restoring the initially observed live reserve to 100%; neither should be silently described as the other. Final restoration to the owner's intended observed baseline is still pending in this checkpoint, with configuration provenance kept separate.

Dev1 exposed reachable defects fixed in `555fa31d` and `ac766794`: confirmed-response paths could return `None`; ordinary manual restore retries forced `self_consumption` instead of preserving the saved mode; numeric target/load presentation could misstate the physical basis; and diagnostic status could remain tied to old recovery ownership. Focused regressions and the final audited pass cover the source corrections. **Their repaired live behavior has not yet been confirmed on dev3 in this report.**

Current Tariff discharge intentionally leaves `battery_ok` when export preparation is untracked. T03 must assess restoration of settings owned by the action and record this export policy explicitly, rather than demanding an original export restoration the current contract does not perform.

## Forty-six-row proof ledger

Every row has software evidence in the exact-node mapping. “Dev1 physical, partial” records an observed physical variant, not completion of every source, provider, limit and lifecycle variant in the [original checklist](tesla-control-path-matrix.md#required-local-home-assistant-verification). No row below grants a dev3 hardware pass.

| ID | Software level / important limit | Live evidence at this checkpoint |
| --- | --- | --- |
| T01 | B: actual cloud route, settings and ownership | Dev1 physical, manual Tariff charge only; optimizer/cloud-only/unreachable-LAN variants pending |
| T02 | B: cloud discharge preparation and refusal | Dev1 physical, manual Tariff discharge; remaining variants pending |
| T03 | B: owned restore, retry and settling | Dev1 tariff restored and native expiry settled; response/manual-mode defects require dev3 retest; final baseline pending |
| T04 | B + L: exact local entry and charge headroom | Dev1 physical 2 kW charging under all three preferences; corrected responses pending |
| T05 | B + L: local export quantity and load/solar | Dev1 physical 1 kW export under all three preferences; corrected basis/presentation pending |
| T06 | B + L: site-import entry, reservation arithmetic | Software only; combined all-preference zero/one/two-EV live/simulator sweep pending |
| T07 | B: Local Max and no tariff fallback | Dev1 physical Max charge/discharge near 10 kW battery output; refusal variant pending |
| T08 | B + L: full inverter-envelope route decision | Dev1 physical native charge/discharge; complete boundary/unsafe-evidence variants pending |
| T09 | B + L: stable episode, owner and deadline | Dev1 handoff refused until settling, then replacement succeeded; other telemetry variants pending |
| T10 | B + A: owned reserve restore; entry/timer wiring narrower | Dev3 dashboard force-to-Hold failed: HA 2026.10 constructor mismatch dropped the nested reserve payload; compatibility fix and final-candidate retest required |
| T11 | B + A: repeated idle-off/expiry restore; full outer timer narrower | Dev3 dashboard Power the home set self_consumption; measured battery 3.75 kW supplying house with near-zero grid at 03:12:15 UTC; five-minute expiry pending |
| T12 | B: manual Auto preference separate from EV leases | Dev3 manual Auto after never left actual never; all export rules then restored battery_ok. EV overlap remains software only |
| M01 | B: manual mode handoff/readback | Dev3 manual backup, self_consumption and autonomous each read back exactly; other provider/unpaired variants software only |
| M02 | B + A: reserve supersession and independent readback | Dev3 standalone reserve 2% then original configured 0% each confirmed; active-force handoff requires constructor-fix retest |
| M03 | B: strict permission and bounded resend | Dev3 disable confirmed. Enable returned unconfirmed after 60 seconds although native omitted-false reading correctly showed enabled. Native write verifier defect reproduced; fix/retest pending |
| M04 | B: export-only confirmed setting | Dev3 never, pv_only and battery_ok each returned settings_confirmed with matching readback; EV acquisition and malformed payloads software only |
| M05 | B: selected-entry on-grid permission and readback | Dev3 permission false then true accepted; true raw native field confirmed, false uses protobuf omission. PV was zero, so no measured curtailment claim |
| M06 | B + L + A: backup event/resolver; complete timer path narrower | Dev3 one-minute native event accepted and expired; same-window numeric discharge also accepted, exposing missing overlap guard. Immediate stop confirmed; fix and reverse-order tests pending |
| M07 | B: actual Storm Watch endpoint, queue and GET | Site reports Storm Watch unsupported; monitoring-blocked source only. Supported account changes remain software only |
| M08 | B: actual off-grid EV reserve endpoint and GET | Site reports off-grid vehicle reserve unsupported; no reserve mutation or unknown-baseline overwrite |
| M09 | B: actual VPP endpoint, forced GET and queue | No VPP enrollment actuation or account-effect verification |
| M10 | B: switch/client/factory pieces; complete local view path narrower | Dev3 already-connected reconnect exercised through service, local API and On-Grid switch; terminal connected readback. No physical islanding |
| M11 | B: signed delay persistence and authenticated view | Read-only unsupported calibration status; no delay actuation |
| E01 | B: two-VIN shared original/final release | No EV actuation |
| E02 | B: restart/reacquisition with durable leases | No EV restart/reacquisition trial |
| E03 | B: failure/cancellation/backoff ownership | Simulator/software only; no injected live uncertainty |
| B01 | B + L + A: writer/readback and source deferrals | Force tariff uploads/restores observed; dedicated scheduled/manual TOU sync sweep pending |
| B02 | B: event baseline, foreign owner and recovery | No AEMO/saving-session live event cycle |
| B03 | B: exact queued predicates/restore handlers; outer path narrower | No poll/websocket solar-curtailment physical cycle |
| B04 | B: kick failure/supersession; direct cancellation narrower | No separately instrumented live kick/cancellation sweep |
| S01 | B: monitoring revocation and responsive reads | Dev3 validation-source charge, discharge and export returned explicit monitoring refusals, with zero mutation receipts; queued revocation software only |
| S02 | B + L: direction-specific protections and cleanup | Max Backup overlap defect found and being fixed; demand/network fault variants software only |
| S03 | B + L: strict unknown/default permission semantics | Known permissions read; no malformed/missing live payload injection |
| S04 | B + L: invalid/stale/off-grid/SOC denial | Simulator/software only for fault variants |
| S05 | B: shared EV/native acquisition and unsettled capacity | No managed/native EV actuation; native settling barrier observed without EV |
| S06 | B: canonical site/foreign owner and other-site progress | No live two-entry/provider plus separate-site concurrency trial |
| R01 | B + L: unresolved signed-write quarantine | No incident deliberately created or reset |
| R02 | B + L: exact supervised cloud handoff/local suspension | No live incident resumption |
| R03 | B: interrupted startup restores without replay | Ordinary dev1/dev3 restart health verified; active-force graceful restart still pending |
| R04 | B: queued cancellation and submitted/unload drain | No queued/submitted unload fault sweep |
| R05 | B: original queue deadline and cleanup exception | Native five-minute expiry observed; delayed queue/provider deadline variants software only |
| R06 | B: uncertain response/cancellation persistence | No live response-loss injection |
| R07 | B: runtime/site/pairing/generation replacement | Software only; no live identity/pairing replacement race |
| R08 | B: responsive reads and retained uncertain effect | Reads observed during active native control; deliberately delayed/unknown provider effect software only |
| D01 | B: actual diagnostic handlers and proven-read envelope allowlist | Cloud/gateway/calibration reads responsive during native control; local DCQ/BMS four-pack health visible. Controlled diagnostic baseline-restoration write pending |
| D02 | B: precise actionable notices and quiet deferrals | No live persistent signed-settings notification cycle |

## Checkpoint closeout and remaining variants

- Save the installation digest receipt, HA configuration check and restart-health result for the exact dev3 candidate. An archive match does not establish a healthy restart.
- Recheck the repaired confirmed-response paths, including blocked calls and local stop/restore, without interpreting a returned dictionary as physical completion.
- Recheck ordinary manual restore retry with a saved autonomous baseline; retain startup/optimizer `self_consumption` policy. A settings/readback delay must retain its owner until cleanup verifies.
- Recheck numeric export target basis, raw versus normalized home load and measured battery/grid flow, plus current diagnostic clearing after recovery finishes.
- Record the final intended mode/reserve, unchanged configured reserve provenance, restored tariff and absence of outstanding force/recovery owners. Owner-proposed restoration of the initially observed live 100% reserve must not overwrite the saved 0% user configuration as an accidental preference change.
- Reconcile the exact 46-row node map against final audited run `9de7bc745483460b98f031a907bd9c72`. Preserve the older reconciliation as historical evidence.
- Keep unperformed EV, islanding, VPP, multi-site, background-event and fault-injection variants explicitly unverified. Use authorized simulation for unsafe uncertainty/outage/storage variants; do not manufacture them on hardware to complete a checkbox.

Publication, installed candidate identity, restart health, direct settings readback and physical results require separate receipts. Later results should amend this checkpoint with their exact artifact, source and UTC window.

## Additional defects found on dev3

The local sweep found three further reachable problems. They invalidate treating this checkpoint as release-ready, despite its passing software suite. Their fixes are implemented and require a fresh complete audit and an exact-artifact local retest:

- A numeric local force can coexist with an active Max Backup event; reverse ordering also lacks entry admission. No deliberate manual cancellation occurs. The observation establishes accepted contradictory controls, not measured reversal during the backup window.
- HA 2026.10 adds `hass` as the first `ServiceCall` constructor argument. Eight older positional constructions silently lose their data. The dashboard Hold test logged a missing reserve parameter and ended before engagement. Both historical and current constructor shapes require coverage.
- The local settings verifier compares omitted native false flags to `None`, although the established native parser correctly decodes that flag as false. This causes an unnecessary cloud fallback and false Grid Charging enable confirmation failure. The fix must stay scoped to a valid native `site_info` payload and leave missing cloud permission unknown.

## Implementation added after the checkpoint

The complete Max Backup review added a verified write-ahead intent to the existing record, canonical site identity, and an acknowledgement phase. Cleanup retains the original baseline/deadline through failed replies, persistence failure, replacement and reload. Unknown native application waits a conservative 60 seconds before cleanup can retire ownership; local signed requests expire after 12 seconds. Queued native events are bounded by the original end time and require acknowledgement of preliminary cancellation. Tariff selects cloud reserve before any native attempt. Ordinary active-window replacement preserves its original baseline; changing route or replacing unresolved cleanup is refused. Legacy or mismatched-site records remain quarantined, with no automatic reassignment or recovery command. This legacy supervised-recovery limitation blocks declaring this work release-ready.

Four additional services used direct global registration and ignored an explicit `entry_id`. Max Backup, Storm Watch, off-grid vehicle reserve and VPP enrollment now use the existing exact-entry router. Their service descriptions expose entry selection. Omitted entry IDs preserve the existing default behavior; invalid or unavailable explicit IDs cannot fall back to another site. Two-entry registration regressions accompany this change.

Nine actual entry/controller tests now compose each of the three preferences with zero, one and two durable EV reservations. They verify distinct site-import and battery targets, settled demand, original deadlines and unchanged configured limits. The preceding source/AST evidence limitations remain separate from these production-behavior cases.

While no PowerSync mutation was recorded, the live reserve returned from 0% to 100% by 03:41 UTC. A fresh native configuration read confirms that value and disabled Grid Charging; the source is unverified. No new PowerSync service/control write appears in the 03:30–03:42 logs. This limits exclusive attribution of later power changes. Monitoring remains enabled and no other controller has been disabled.
