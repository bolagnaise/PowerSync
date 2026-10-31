# Tesla control validation — 31 October 2026

**Checkpoint scope after concurrent main update:** the detailed receipts below
record candidate7 and its frozen Tesla implementation. The branch subsequently
integrates main commit `dbfab9dde2795dade6c94b814307392dc0dd9566`, which changes
rolling tariff slot display to HA local time and adds five sensor tests.
Tesla actuator source is unchanged. The combined sensor documentation signature
was reviewed again; the post-merge full-audit and candidate8 installation
receipts are maintained in [PR #138](https://github.com/bolagnaise/PowerSync/pull/138).


**Status: implemented and source-validated; unreleased.** Published baseline is
`origin/main` `904614102e1458b1d66ba99579bbb429f08a071b`, version `v2.12.1574`. Source
`fd29787fab232030eda3ec72681294ac8997c5db` includes that baseline and the
feature changes under review; those feature changes are not merged into
that public baseline. The tested checkpoint candidate is `2.12.1575.dev7`, built
from `759c67bf0533902ee982d6ed088990e0593d82d4`. Candidate7 changes only the
sensor description to **`Battery hold active`** and the manifest relative to
dev6; the actuator source is unchanged. Candidate7 was installed at 04:58:14.

FULL10 passed **9,714/9,714**, zero skipped, in 703.47 seconds (run
`06c00a6ebe0d4d3ba5a660a3ada58b10`). FULL11 for dev6 passed **9,714/9,714**,
zero skipped, in 593.58 seconds (run
`d08760cf221a486793cba1233b564ed3`). Both have node hash
`7217ac1f4554db367158adbf865735bb3af19ef7092fe9698657656d49a7f255`.
FULL12 for candidate7 passed **9,714/9,714**, zero skipped, in 465.76 seconds
(run `549c5e4386114223b84581f688990465`). The audited run is bound to frozen
source `759c67bf0533902ee982d6ed088990e0593d82d4`; later report-only commits
do not change the installed package. No public release is recorded here.

## Candidate and installation provenance

| Item | Recorded evidence |
| --- | --- |
| Published baseline | `origin/main` `904614102e1458b1d66ba99579bbb429f08a071b`, `v2.12.1574` |
| Functional source checkpoint | `fd29787fab232030eda3ec72681294ac8997c5db`; includes the published baseline but is not merged into it |
| Dev5 archive | 43,749,948 bytes / 264 files; SHA-256 `98b678a0e8f5c65d9538d5be2b7b6e12853a1304dad316257b5d5ccc268c12d9`; installed with matching file hashes |
| Dev6 archive | 43,749,949 bytes / 264 files; SHA-256 `59eef023a46d4f86b5a10fa009e8d876b8ef79b0dc9700f0ef90af774e935d18`; installed on the authorized HA instance |
| Dev6 source | `fe7453f35cbeee4e547f654cf27834e45e123a36` |
| Dev5/dev6 equivalence | 886 individual function ASTs identical; only manifest and `__init__` warning bytes differ |
| Tested candidate7 checkpoint | `2.12.1575.dev7`, source `759c67bf0533902ee982d6ed088990e0593d82d4`, 43,749,943 bytes / 264 files, SHA-256 `0510149dea96cc525bf4e1d9c808b069178c3c92320afb9fad526bb8e8ab909a` |
| Dev6/dev7 comparison | Only the sensor description (`Battery hold active`) and manifest differ; actuator source is unchanged |
| Candidate7 installation | Installed at 04:58:14; all 264 archive hashes matched; Core check and startup were healthy |
| FULL12 | Complete: 9,714 passed, zero skipped, four audited workers |

Evidence is retained under `/tmp/powersync-tesla-control-evidence/`, including
`matrix-run10-reconciliation.{json,md}`, `ha-dev4-final-records.json`, and
`ha-dev5-final-records.json`, `ha-terminal-dev7-records.json` (211 records),
`final-installed-health.json`, `final-obligations.json`, and final baseline
receipts. Credentials and full entry/site identities are
omitted from this report.

## Control architecture and preserved boundaries

The [control-path matrix](tesla-control-path-matrix.md) inventories production
writers and admission obligations. The implementation separates requested
quantity, route selection, ownership, mutation submission, provider receipt,
settings readback, physical settling, and observed result:

1. `NativeMaxTarget`, `BatteryPowerTarget`, `GridExportTarget` and
   `SiteImportTarget` preserve different physical meanings. Explicit positive
   watt requests remain local under all three saved preferences. Tariff Max is
   not a precise watt target. Automatic evaluates the full inverter envelope
   and retains an active native episode until cleanup.
2. `PowerwallSiteControl` is shared by the canonical physical energy-site
   identity across entries and providers. `route_scope(entry_id)` reserves
   preparation as well as the route lock, so a foreign writer cannot slip in
   before an event or force action publishes its active state. Same-entry
   cleanup remains possible; different sites retain independent progress.
3. The controlled HTTP session gates energy-site mutations, including generic
   `request()` and retries. Local signed transport gates mutations and complete
   configuration read-modify-write transactions; only proven reads bypass this
   boundary. Runtime, identity, ownership, and original deadline are checked
   after queue wait and before the raw driver.
4. Force, background event, EV, and Max Backup workflows retain restoration
   owners and durable baselines. A cancelled or lost submitted response stays
   uncertain until the recovery contract resolves it.
5. An unresolved signed-settings incident blocks conflicting writes. Supervised
   cloud resumption retains that incident and its restore basis while local
   signed settings remain suspended. Monitoring, licensing, demand/network
   protection, Max Backup, SOC, and EV capacity gates retain separate authority.
6. Native admission preserves structured reasons. Actionable persistent
   blockers can notify; routine SOC, EV, monitoring, startup, and restoration
   deferrals stay quiet. Diagnostics derive current ownership rather than a
   historical receipt.
7. A callback, service response, provider acknowledgement, direct readback,
   physical settling, and HA presentation are separate outcomes. Hold cleanup
   intentionally ends in `self_consumption`; it is not an autonomous-mode
   restoration claim.

With Tariff selected, ordinary mode, Grid Charging, and Grid Export settings
use cloud control without a local-write attempt. Backup reserve and tariff
uploads remain cloud operations. Local and Automatic retain guarded local or
settings fallback behavior. No preference discards a restoration or signed-write
obligation.

## Audited software evidence

The FULL10, FULL11 and FULL12 runs used the audited four-worker runner. Their exact
summaries are retained in the operational evidence directory.

| Run | Source | Run ID | Complete / passed / skipped | Workers / elapsed | Node hash |
| --- | --- | --- | --- | --- | --- |
| FULL10 | `fd29787...` | `06c00a6ebe0d4d3ba5a660a3ada58b10` | `true / 9714 / 0` | `4 / 703.47 s` | `7217ac1f4554db367158adbf865735bb3af19ef7092fe9698657656d49a7f255` |
| FULL11 | `fe7453f35cbeee4e547f654cf27834e45e123a36` | `d08760cf221a486793cba1233b564ed3` | `true / 9714 / 0` | `4 / 593.58 s` | `7217ac1f4554db367158adbf865735bb3af19ef7092fe9698657656d49a7f255` |
| FULL12 | `759c67bf0533902ee982d6ed088990e0593d82d4` | `549c5e4386114223b84581f688990465` | `true / 9714 / 0` | `4 / 465.76 s` | `7217ac1f4554db367158adbf865735bb3af19ef7092fe9698657656d49a7f255` |

The row reconciliation maps 466 historical associations through explicit M06A
and R03 selector replacements to **468 passing associations across 426 unique
nodes**: **398 B** (production behavior with fake I/O), **63 L** (pure logic),
and **7 A** (source/AST wiring). The final reconciliation also verifies **349 supplementary passing nodes**,
346 additional to that map, including 36 transport, 27 Hold, 50 recovery and
nine composed preference/EV-budget B cases. All four ordered collections and
all **29,142** passing setup/call/teardown phase reports were validated.
Exact receipts are `matrix-final-reconciliation.{json,md}` and
`/tmp/powersync-tesla-control-full-20261031-12/{summary,audit}.json`. Fake I/O cannot establish a real provider
acknowledgement, device readback, account effect, physical settling, or
measured battery/grid response. The companion matrix retains the complete
scenario review and focused test package.

## Forty-six-row live assessment

The B/L/A column is the reconciled software association count. Each live or
simulator boundary remains explicit; no row is a universal physical pass.

| ID | Acceptance scenario | B/L/A | Software assessment | Local-site result and remaining limit |
| --- | --- | --- | --- | --- |
| T01 | Tariff charge, manual/optimizer, cloud-only and unreachable-LAN variants | B16 | Real route handler selects cloud without reading unreachable LAN. | Dev1 manual Tariff Max measured 11.04 kW charge / 14.75 kW import. Optimizer execution, other providers and intentionally unreachable LAN remain software-only. |
| T02 | Tariff discharge, manual/optimizer, cloud-only and unreachable-LAN variants | B18 | Cloud-only discharge route distinguishes export receipt from permission confirmation and can revoke admission during grid confirmation. | Dev1 manual Tariff Max measured 14.25 kW discharge / 10.10 kW export. Optimizer, other-provider and unreachable-LAN variants remain software-only. |
| T03 | Stop/expiry/restore after charge and discharge | B5 | Captured tariff and complete owned-setting restore retain ownership through settling. | Dev1 charge/discharge stop and native natural expiry settled; original tariff hash restored. Dev5/6 stop and restart cleanup passed. Fault variants remain simulated. |
| T04 | Numeric battery charging under Tariff, Local, and Automatic | B6/L1 | Each preference uses precise local numeric charge entry; headroom caps battery input. | Dev1 all three preferences measured 1.998–2.001 kW charge for 2 kW requests. Dev5 final actuator source confirmed 2 kW charge and stop; no three-preference final-artifact physical rerun. |
| T05 | Numeric grid-export target under all three preferences | B9/L1 | Local numeric discharge accounts for measured home and solar power. | Dev1 all preferences measured approximately 1 kW grid export. Dev6 final actuator source measured -985 W grid with grid_export target basis, then stopped to house load. |
| T06 | Site-import/spread target with zero, one, or two EV reservations | B5/L1 | Spread target remains a site-import target; EV watts constrain renewals without changing deadline/configuration. | Composed production-controller software cases cover three preferences and zero/one/two EV reservations. No multi-VIN physical reservation trial was performed. |
| T07 | Local Max charge/discharge and local refusal | B3 | Local preference cannot silently fall through to Tariff; release lifecycle is covered. | Dev1 Local Max measured about 9.997/9.996 kW charge/discharge. Final refusal paths are software-tested; no manufactured gateway failure live. |
| T08 | Automatic route on both sides of the inverter envelope | B2/L6 | Route decisions cover complete inverter flow and unsafe evidence; native lifetime is exercised. | Dev1 Automatic native charge/discharge measured about 10.98/14 kW; explicit numeric stayed local. All envelope boundaries remain software-tested, not physically swept. |
| T09 | Load/telemetry changes during an Automatic native episode | B4/L1 | Running native route retains its owner and only shortens its original deadline. | Dev1 native-to-numeric replacement was refused until physical settling, then accepted; original native deadline retained. Complete telemetry permutations remain simulated. |
| T10 | Hold SOC release/expiry during idle and force transitions | B1/A2 | Hold cleanup restores only owned reserve; source wiring preserves the target through the pulse. | Dev5 actual UI Hold naturally expired with exact reserve 0, owner cleared, house power restored. Dev7 UI label and manual release confirmed. Hold is a reserve floor and may charge. |
| T11 | Self-consumption/idle entry, off, and expiry | B3/A1 | Restore handler avoids repeated historical uploads and releases the self-consumption override; timer wiring is asserted. | Dev3 actual UI Power the home entered self-consumption, served about 3.75 kW house and naturally expired to autonomous. Remaining interrupted timer variants are software-only. |
| T12 | Return export preference to Auto during manual/EV ownership | B4 | Auto service targets the selected entry and preserves EV ownership. | Dev3 manual export-to-Auto cleared the manual flag. No two-VIN installed lease interaction was exercised. |
| M01 | Manual operation-mode handoff/readback | B4 | Old mode is restored before a new setting; local/cloud fallback is guarded. | Dev3 modes backup/self_consumption/autonomous confirmed. Dev7 autonomous restored. Other-provider/unpaired handoffs remain software-only. |
| M02 | Backup reserve changes during idle and force handoff | B22/A1 | Reserve supersession stops stale targets and uses independent cloud readback. | Dev3 idle reserve 2→0 confirmed. Dev5 reserve 2 during active charge survived stop; canonical 0 restored afterward. Native/cloud readback limits are recorded in final baseline. |
| M03 | Grid-charging enable/disable and absent-field compatibility | B6 | Omitted/invalid values remain unknown; disable confirmation and bounded resend are covered. | Dev4 false→true permission confirmed in about one second using corrected native parsing. Missing/malformed cloud permission remains software-only. |
| M04 | Export `never`, `pv_only`, `battery_ok`, and invalid/absent input | B20 | Exact field confirmation preserves EV ownership and cannot substitute reserve/mode writes. | Dev3 never/pv_only/battery_ok readbacks confirmed; final battery_ok confirmed native. Invalid/absent and overlapping EV variants remain simulated. |
| M05 | Selected-entry on-grid solar permission | B5 | Service checks entry, manual context, fresh native setting, and revocation inside delayed write. | Dev3 native solar permission false→true confirmed. PV was zero, so measured curtail/resume output is unverified. |
| M06 | Max Backup entry/exit and overlap refusal | B1/L10/A1 | Restore target and local-event wiring are covered; native policy denies overlap. | Dev4 Tariff one-minute entry/expiry restored reserve and journal. Dev5 native start, measured charge, early cancellation, fresh absence and cleanup passed; four overlap requests refused. Legacy recovery refusal/cleanup also exercised. |
| M07 | Storm Watch request and direct status refresh | B9 | Generic endpoint setter/getter uses the shared gate and exact payload/readback contract. | Explicit invalid entry refused live without fallback. Successful Storm Watch account change was not performed; setter/getter and gate covered in software. |
| M08 | Off-grid vehicle reserve request | B10 | Generic endpoint setter/getter uses the shared gate and independent field contract. | Explicit invalid entry refused live without fallback. Successful off-grid EV reserve account mutation was not performed; independent field behavior covered in software. |
| M09 | VPP enrollment in an authorized test environment | B10 | Generic endpoint path and gate are covered. | Invalid entry refused live. No VPP enrollment or program effect was attempted; authorized-account behavior remains unverified physically. |
| M10 | Authorized island/reconnect through service, switch, and local view | B5 | Pending state, local terminal confirmation, cloud fallback, and identity binding are covered. | Dev3 already-connected reconnect via service, switch and API confirmed. Physical islanding and actual grid reconnection were not attempted. |
| M11 | Calibration delay request/status and reload | B4 | Idempotent delay and short acceptance barrier persist through reload/crash; status remains readable. | Read-only native calibration status returned unsupported firmware status 12. No calibration-delay mutation or physical calibration was attempted. |
| E01 | Two-VIN Smart Schedule staggered release | B2 | Two leases share one original and only the final owner restores it. | Two-VIN acquisition/staggered release verified with production behavior and fake I/O. No two-VIN physical trial. |
| E02 | Restart with saved leases and one VIN reacquiring | B3 | Restart recovery permits reacquisition while another lease releases without duplicate capture. | Durable EV lease restart/reacquisition verified in software. No installed two-VIN restart trial. |
| E03 | EV export failure/cancellation/ack loss | B8 | Failure cannot acquire; cancellation remains durable and restore retries are bounded. | Loss/cancellation/backoff retained in simulated production behavior. No uncertain hardware write was manufactured. |
| B01 | TOU sync during idle, force, monitoring, and restore | B2/L1/A2 | Tariff writer has final predicate/fence and uncached readback; source asserts ownership deferral. | Dev3/4 manual sync during Monitoring and Hold produced no new mutation; original tariff hash unchanged. Allowed scheduled-upload permutations remain software-only. |
| B02 | AEMO spike and saving-session enter/exit/retry | B7 | Event classes retain baseline and ownership across failed restoration. | Scoped event entry/exit/retry and retained baseline verified with fake I/O. No real saving-session/AEMO trigger was fabricated. |
| B03 | Poll/websocket curtail/resume with EV/manual override | B47 | Final predicates execute through the shared queue and preserve manual `never`. | Final predicates/queues verified in software; explicit native solar permission exercised. Full poll/websocket PV curtail/resume with EV overlap remains physically unverified. |
| B04 | Charge kick/bounce completion, replacement, cancellation | B12 | Lifecycle covers bounce/readback/grid failure/newer command and queued cleanup generation. | Bounce/replacement/cancellation verified in production coroutine tests. No deliberate hardware fault or vehicle command was issued. |
| S01 | Monitoring takeover while a request waits | B19 | Local renewal/event queue stop at monitoring loss while diagnostic reads remain available. | Dev3 validation-source charge/discharge/export requests refused under Monitoring with zero new effects; owner manual cleanup remained allowed. Queued takeover race is software-only. |
| S02 | Demand/network protection and Max Backup overlap | B7/L1 | Direction-specific denials and queued rechecks preserve protection. | Dev4/5 four Max Backup conflicts refused without effects. Demand/network direction and delayed protection revocation remain software-tested. |
| S03 | Disabled/unknown grid charge/export permissions | B6/L7 | Invalid booleans remain unknown; native unknown permission denies before write. | Known grid/export values confirmed live; unknown/malformed permissions refuse in software. No permission was inferred from missing cloud data. |
| S04 | Stale/invalid/off-grid/SOC floor/cap variants | B10/L24 | Invalid-physics matrix and local controller deny before mutation; native refuses absent capacity. | Stale/invalid/off-grid/SOC boundaries verified in software. No physical islanding, unsafe SOC sweep or telemetry corruption was performed. |
| S05 | Managed/native EV activity and pending capacity | B9 | Shared ownership and connection budget prevent contradictory native Max. | Native/managed EV ownership and budget cases verified in software. A later approved Manual trial physically charged one car; the second car's BLE command was unconfirmed and Fleet rejected its key. No physical two-VIN shared-capacity experiment passed. See the [two-vehicle supplement](tesla-two-vehicle-validation-2026-10-31.md). |
| S06 | Same site through two entries/providers plus another site | B7 | Canonical aliases serialize one site while another progresses independently; foreign ownership is fenced. | Shared-site aliases and independent-site progress verified in software. One physical site/provider available, so physical multi-site/provider proof remains unverified. |
| R01 | Existing unresolved local signed-settings incident | B2/L9 | Lost signed response retains obligation; cloud gate and native admission remain blocked. | Signed-loss quarantine verified in software. No new unknown signed-write incident was created on hardware. |
| R02 | Supervised cloud resumption then restart | B5/L1 | Cloud-only handoff and durable restart recovery retain local suspension. | Supervised cloud-resumption/restart behavior verified in software. No genuine unresolved signed-settings incident was available for live handoff. |
| R03 | Restart during active/local/native/interrupted preparation | B8 | Captured cleanup runs without replay; startup waits on native fence and failed cleanup remains gated. | Dev5→6 upgrade retained an actual active local-force record; startup cleared it before original deadline without replay. Native/interrupted-prepare loss variants remain simulated. |
| R04 | Reload/unload with queued/submitted work | B3 | Queued cancellation cannot send later; submitted I/O remains uncertain and terminal persistence is owned. | Dev6→7 idle stop/start healthy; active-force stop/update/start covered R03. Queued/submitted uncertainty and terminal persistence remain software-only fault cases. |
| R05 | Queue/read/provider wait crossing original deadline | B7 | Every initial/resend enforces the original deadline; cleanup can continue after forcing expires. | Dev1 native and Dev5 Hold natural deadlines observed; original native backup end preserved through early cleanup. Artificial provider/queue deadline waits remain simulated. |
| R06 | Cancellation/response loss after submission | B4 | Unknown effect persists through restart and cannot be erased by unrelated success. | Response-loss/cancellation uncertainty verified in software; not injected into hardware. |
| R07 | Entry/site/pairing/generation replacement during await | B11 | Callbacks are fenced by binding, site, runtime, and pairing identity. | Site/runtime/pairing/store/replacement predicates verified in software. No owner-site re-pair or site rebinding was performed. |
| R08 | Responsive reads and stop while provider reply is delayed | B7 | Shared gates leave reads free; callback error keeps unknown status visible. | Live diagnostic reads remained responsive during force/recovery. Deliberately delayed provider plus stop/unknown-record interaction remains software-only. |
| D01 | Read-only diagnostics and authorized diagnostic mutations | B18 | Diagnostic handlers enforce runtime/site/foreign revocation; proven reads remain free. | Local/cloud/GET and native config diagnostics exercised; controlled backup probe restored original observed reserve. Unsupported calibration status noted. Query classification and revocation covered in software. |
| D02 | Repeated actionable denial then successful admission | B22 | Actionable settings reasons deduplicate; routine SOC/EV/monitoring/restore waits stay quiet and clear after admission. | Actual retained Max Backup recovery diagnostics and actionable notice exercised and cleared. Repeated signed-loss denial/push variants remain software-only. |

## FULL11 dev6 checkpoint

This is the separate dev6 checkpoint used before freezing candidate7. Dev6 was
installed and restarted on the authorized HA instance. The active local-force
request began at 04:50:00 with software deadline 04:55:01.025 and measured
about -2012 W battery / 4095 W grid at 04:50:05. Core stopped at 04:50:17,
restarted at 04:51:49, and at 04:52:12 showed about 2078 W house / -8 W grid
with no replay or owner. The store was clear at 04:52:22.

The 1 kW export request used a `grid_export` basis at 04:52:45. At 04:53:50
the sample was about 3159 W battery, -985 W grid, and 2168 W home. Stop at
04:54:11 succeeded; at 04:57:05 readings were about 825 W battery, 6 W grid,
and 820 W home, with no owner. These are sampled live results for dev6 and do
not physically validate every row.

## Candidate7 installation checkpoint

Candidate7 was installed at 04:58:14. All 264 archive hashes matched, and the
Core configuration check and startup were healthy. At 04:59:20.730 the actual Hold
entity presented the description `Battery hold active`. Manual release at
04:59:30 succeeded with reserve 0 and `self_consumption`; sampled readings were
about 959 W house and -3 W grid. Setting autonomous at 04:59:42 returned a
confirmed response. An original-reserve 100% restore POST at 04:59:57 returned
HTTP 200; the fresh-read result remains part of the final baseline evidence.

## Dev4–dev6 live observations

The captures concern one authorized HA installation, one configured Tesla
provider, and one paired gateway. Values are samples. Negative battery power is
charging; negative grid power is export.

- Grid Charging false-to-true was confirmed in about one second. Manual mode,
  export, solar permission, and reconnect routes remain source/artifact-qualified
  checks.
- Dev4 Hold natural expiry at 04:11:26 exposed a missing cleanup marker. The
  owner remained until a manual retry cleared it. The fix covers all four Hold
  timer/retry markers and adds a shielded child physical-identity fence.
- Dev4 Tariff Max Backup ran 04:13:49–04:14:49, restored reserve 0, cleared its
  journal, and produced about 12.7 kW charge before returning to house load.
  Four force/Hold/self-consumption conflicts were refused without effect.
- A fresh typed-empty native start skips cancellation immediately. The 60-second
  drain applies to uncertain cleanup; unknown or present state still requires
  acknowledgement. `recover_max_backup` is cleanup-only and requires exact
  entry/site/record identity plus acknowledgement. Missing-site legacy records
  need attestation; an explicit wrong site cannot rebind. Native-only saved
  reserve `None` permits native cleanup without a reserve actuator write.
- Dev5 recovery with no acknowledgement or a stale receipt refused with zero
  effects. Valid recovery at 04:37:02 returned `cleanup_pending`; after 60
  seconds, a fresh manual-`none` read cleared the durable journal without an
  actuator write and left the original record/end unchanged.
- Dev5 Hold at 04:39:11 engaged 69% reserve and `self_consumption`, saved user
  reserve 0, settled to 0 W by 04:44:11, and naturally expired at 04:44:19
  through an initial pulse of 1 to exact target 0 at 04:44:25. Final readings
  were about 2073 W house and 6 W grid, with no force journal, native event, or
  pending owner. During Hold, about 5.5 kW grid charging with zero PV occurred
  before the floor. This is not a strict-lock proof; the corrected warning
  describes the 80% reserve-floor behavior and possible solar/grid charging.
- Dev5 Native Max Backup ACKed at 04:44:35. Physical readings at 04:45:24 were
  about -10,999 W battery, 13,140 W grid, and 2,148 W home. Recovery waited 60
  seconds; native cancellation ACKed at 04:46:24.859 before the original end.
  At 04:46:32.476 manual was `None` with no fresh native event; the journal
  cleared by 04:47:23 at about 1991 W house, -5 W grid, reserve 0, autonomous.
- HA reserve 2 during active 2 kW charge succeeded at 04:48:06. Stop Normal at
  04:48:20 preserved 2 and cleared the force owner; HA reserve 0 at 04:48:58
  restored canonical 0.
- The active-force restart check used a 2 kW request at 04:50:00, deadline
  04:55:01.025, and about -2012 W battery / 4095 W grid at 04:50:05. Core
  stopped at 04:50:17 for the dev6 upgrade, restarted at 04:51:49, and at
  04:52:12 showed about 2078 W house / -8 W grid with no replay or owner. The
  store was clear at 04:52:22.
- Dev6 1 kW export used a `grid_export` basis at 04:52:45. At 04:53:50 the
  sample was about 3159 W battery, -985 W grid, and 2168 W home. Stop at
  04:54:11 succeeded; at 04:57:05 readings were about 825 W battery, 6 W
  grid, 820 W home, with no owner.

These captures do not physically verify all 46 rows, providers, firmware,
EV multi-VIN behavior, islanding, VPP, other-provider paths, or unsafe fault
variants. A tag, accepted API write, journal state, callback, dashboard state,
or cloud readback is not physical-effect proof.

## Final audit and site baseline

- **Final audit:** FULL12 and the independent mapping reconciliation both
  passed on the frozen candidate7 source, with the exact receipts above.
- **Final baseline, 05:03:50 UTC:** native config and HA settings both show
  autonomous mode, backup reserve 100, `battery_ok`, and grid charging enabled;
  native on-grid solar permission is true, curtailment and manual export
  override are false, Storm Watch is false, grid is connected, and method is
  Automatic. Monitoring remains enabled. Original canonical user reserve 0,
  optimizer floor and configured import/export limits are unchanged.
  The controlled reserve restore returned HTTP 200 and provider `Code: 200`;
  native readback was 100 at 05:00:27, while cloud settings remained 0 until
  the later read. This readback delay is recorded separately from acceptance.
  The final sample is approximately 13.808 kW battery charging, 14.758 kW grid
  import and 0.951 kW home load under the restored reserve; there is no active
  PowerSync force/native/backup owner or uncertain/pending settings record.
  Independent cloud baseline comparison also matches all originally captured
  settings, and the original FOUR4FREE tariff hash remains
  `70238c7bc304c8e74fb5d657413a419838b222efcaecf080dea8f35bf65e8e60`.
  All 264 installed file hashes match candidate7. Core configuration check and
  start succeeded; Docker reports running since 04:58:31.249 UTC, with zero
  PowerSync errors in the captured startup window. Temporary logging and the
  read-only event listener were restored/stopped; the temporary auth file is
  absent. Original backup and each candidate rollback directory are retained.
- **Hold label:** Verified at 04:59:20.730 as `Battery hold active` on the
  installed candidate7 entity.

The feature remains in draft PR [#138](https://github.com/bolagnaise/PowerSync/pull/138).
The 46-row review is complete at the evidence levels recorded above; complete
physical coverage is not claimed. Publication remains held because the local
site cannot establish the multi-site/provider, two-VIN, account/program and
unsafe fault variants. Any eventual publication must also obtain protected-main
matching full-suite success through the tracked release workflow. The known
legacy recovery blocker is not outstanding: cleanup-only recovery, typed-empty
handling, persistence, identity fencing, and diagnostics are implemented and
covered by the current software and dev4–dev6 evidence.
