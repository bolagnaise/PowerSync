# Paired Powerwall charge and export targets

Paired Powerwalls with a reachable LAN gateway use timed local battery control for numeric charge/discharge targets. The Tesla-only Preferred battery control selector chooses the method for Max and optimiser actions, with Tariff as the default for existing and new sites. Charge kW targets battery input. Discharge kW targets grid export, adjusting battery output for current home load and solar. Fresh telemetry, reserves, charging permission, configured power limits and gateway site limits still apply.

From v2.12.1494, an empty gateway telemetry reply is retried once before a control fails. PowerSync reads both live telemetry and gateway limits again; it never uses cached readings or the first attempt's limits to start control. Pairing rejection and connection timeouts still fail immediately. If telemetry or required limits remain unavailable, charge/discharge remains blocked.

## Preferred battery control

The Tesla-only **Preferred battery control** selector is saved per Home Assistant entry and shared with the mobile app. It is available independently of an optimiser subscription; actual controls retain their existing admission checks.

- **Tariff** (default): use the Tesla cloud API for Max and optimiser charge/export. A paired gateway being unreachable does not block tariff control. Mode, grid-charging permission, reserve and tariff writes retain their cloud acknowledgement/readback and restore checks; no local startup kick is required.
- **Local V1R**: use timed gateway commands capped at 10 kW or a lower learned charge/discharge ceiling. This requires pairing and fails explicitly if local control is unavailable; it does not silently upload a tariff.
- **Automatic**: choose charge and discharge independently using fresh home load, solar, connection limits and available battery power. A multi-Powerwall site can therefore use Local V1R for a modest export and Tariff for charging. Missing or unsafe telemetry selects Tariff, whose own safety checks must still pass. A running native tariff action retains its route until restored.

Explicit positive kW requests retain local wattage control regardless of this preference. They cannot become unrestricted tariff actions. Local inverter output includes home consumption as well as export, so the same grid-export target may fit the local ceiling at one load and exceed it at another.

Changing the preference stops the current force action and confirms restoration before saving. Pending or failed cleanup keeps the previous preference. The optimiser requests a fresh plan; a stopped manual action is not replayed. Mobile reads the saved backend preference and hides the selector when an older backend does not advertise support.

## Gateway command limits

The gateway can report a timed-command limit below the combined Powerwall rating. From v2.12.1477, PowerSync learns charge and discharge ceilings independently from an exact matching gateway rejection. It requests normal operation, requires the gateway’s acknowledgement, and permits one retry with fresh safety checks inside the original time window. Later commands and the manual power controls use that ceiling; the configured battery rating is unchanged. An unconfirmed resume blocks further dispatch. Tariff fallback cannot bypass a learned ceiling or enforce a selected kW target.

A four-Powerwall 2 site on firmware 26.34.0 reported a 10 kW timed charging ceiling despite its configured 20 kW combined rating. The reporter demonstrated 10 kW charging and 10 kW grid export. This is evidence for that system, not a universal Powerwall 2 limit or a guarantee for other firmware. Check measured battery and grid power; acknowledgement alone does not prove output.

## Startup kick and tariff control

When Automatic selects the native tariff route, Max charge/discharge and optimiser actions use the existing tariff method for sustained power. When safe, one local startup command requests at most 10 kW for at most 30 seconds. PowerSync immediately releases that command and requires acknowledgement before applying the tariff. It does not renew the startup command or restart the action's finish time. A brief transition in measured power is possible; this does not promise seamless takeover. Explicit Tariff uses the cloud path without acquiring local control.

Tariff control asks Tesla to charge or export through its own operating mode and prices; it does not enforce an exact software watt target. Tesla's site limits remain effective. Numeric targets still require fresh local telemetry. Unfinished local/native restoration, an existing native-control owner, or reserved shared EV charging headroom can still prevent takeover; an unreachable gateway alone cannot.

The startup command is skipped if current battery flow, solar output or net home demand makes it restrictive, or the requested direction is already underway. Numeric targets remain bounded local commands. Unknown or expired site-capacity data cannot authorize a larger native planning envelope; paired local commands remain bounded by 10 kW, lower reported ceilings and current safety limits.

Tariff-routed optimiser plans use tariff capabilities rather than inheriting the local 10 kW ceiling. Exact numeric requests never silently become unrestricted tariff control. Optimiser charge/discharge settings are planning rates, not exact native power ceilings. A rounded planning rating below the reported site rating does not block targetless tariff control. Reserve and charge-cap duration checks use the higher reported/configured rating, so a planned window may be shortened to avoid crossing the software SOC floor.

From v2.12.1486, native discharge temporarily tightens a looser gateway export limit to the configured PowerSync export limit and verifies readback before uploading a forcing tariff. For example, a 10.1 kW gateway limit becomes 10.0 kW during a 10.0 kW software-limited action. A stricter existing gateway limit is retained. PowerSync keeps the temporary limit through physical settling, then restores the captured value only if it still owns that field; external edits are preserved. Failed readback blocks forcing. Grid import limits and conflicting EV ownership can still block native takeover. Tariff-routed Max reports a failed takeover instead of silently starting a capped local command.

A gateway's explicit unset export-limit value can also be tightened to the configured PowerSync cap before native discharge. Missing, invalid or zero export limits still block the action. The original unset value is restored only after the same ownership and physical-settling checks.

From v2.12.1488, PowerSync also validates the fresh export-limit snapshot used to capture the restore baseline. Missing, null or malformed capture evidence blocks the action. Invalid saved limits or restoration readback retain cleanup for retry, rather than being treated as an external edit or a completed restore. Sites without a software export cap retain their existing native-limit behavior.

The kick and tariff share one restoration owner and saved settings. Stop, expiry, restart or a changed direction revokes pending forcing writes, releases local dispatch if necessary and restores owned settings. Battery Mode exposes `control_path`, `command_status`, `startup_kick_w` and `handoff_phase`. These describe requests and acknowledgements; Battery Power and Grid Power establish the physical result. Discharge cleanup also waits for fresh power observations before releasing ownership. A repeated optimiser action can shorten the durable native deadline without another startup command or tariff upload; it cannot extend that deadline. Restart restores the retained action rather than replaying it.

## Native Max charging (from v2.12.1482)

From v2.12.1482, Home Assistant distinguishes **Max** from a numeric battery-input target. For a manual or automation `force_charge` action, zero or omitted `power_w` requests Tesla-managed maximum charging. An eligible paired system resumes local dispatch, temporarily uses a charging tariff and Time-Based Control, and restores its original tariff, mode and reserve afterwards. No extra enable switch is needed. Max can exceed the optimiser's configured charge planning rate; numeric targets keep local wattage control. The preferred control method now determines the optimiser route; tariff-routed requests retain their energy and reserve checks.

Grid-charging permission, SOC protection, demand protection, Monitoring Mode and other controller ownership still apply. A finite PowerSync import limit permits native Max only when a fresh gateway import limit is no higher. Active or unsettled EV control keeps charging on the local path, where prospective EV demand can be reserved. Max therefore does not bypass a lower software import limit or an EV reservation.

The timed-command ceiling is separate from native charging capability. An owner-authorized PW3 test reached 13.82 kW battery charging with 0.94 kW home load and 14.76 kW grid import, retaining the site's 14.9 kW import limit. The only observed `config.json` changes were mode and reserve; there was no adjustable 10 kW dispatch-limit setting. This demonstrates charging above 10 kW on that site, rather than a universal charge rating.

Native TOU control can take roughly a minute to affect measured power, including after restoring settings. Acknowledgement or restored settings do not prove charging has physically stopped. Keep checking Battery Power and Grid Power. This path relies on Home Assistant for timed restoration; it does not have the short device-side lease used by numeric local dispatch. Its restore journal survives restart, and uncertain cleanup blocks a new control owner. Manual changes are preserved while PowerSync removes the remaining settings it owns.

After restoring settings, PowerSync retains its charging reservation until two fresh, balanced readings show charging has returned to its pre-Max level, allowing a small measurement tolerance. Restart repeats those observations. This prevents an EV controller from reusing capacity while the additional Max charging is still settling.

A local HTTP timeout does not expire a signed gateway command. PowerSync records the actual signature deadline before sending a configuration write and closes that acceptance window before reconciling settings. An uncertain Fleet tariff upload has no equivalent known expiry: it remains pending until its submitted tariff can be reconciled. Elapsed time or an unchanged tariff alone does not authorize a competing controller.

Before native Max captures its restore tariff, PowerSync waits for an earlier price-tariff upload and its readback to finish. New price uploads defer during this handoff, so a PowerSync price refresh cannot replace the saved baseline during acquisition. The original request deadline still applies, and tariffs changed by another controller remain protected.

Cloud Max charging has a separate implementation and delivery history. This section describes the Home Assistant control path.

## Full battery and cached charging plans

From v2.12.1479, the optimiser checks fresh paired-gateway SOC before applying or extending a charge. At the configured charge ceiling, including 100%, it restores normal operation once and suppresses repeated cached charge attempts. It can resume charging when a later fresh reading is below the ceiling. Stale, failed or invalid local readings do not establish that the battery is full. The local command retains its independent safety check.

Optimiser charge logs label the requested watts explicitly. The local acknowledgement reports the accepted battery target separately; measured battery/grid power establishes the physical result. A 20 kW request followed by a 10 kW acknowledgement is not proof of 20 kW charging.

## Spread Import, Spread Export and EV charging

Home Assistant's Spread Import distributes battery charging across eligible same-price windows. Spread Export distributes planned export across eligible windows. They retain reserve, price, ownership and connection limits; Cloud does not expose these HA switches.

The optimiser already plans PowerSync-managed EV charging alongside the battery and treats supported native Tesla charge windows as fixed forecast demand without taking vehicle control. Spread Import now reserves the managed EV's planned power when redistributing battery energy, in addition to native Tesla demand already included in the load forecast. These sources are counted separately, once each.

From v2.12.1482, an optional native-window vehicle rate controller can share local battery headroom. Its default-off permission, supported windows and restoration rules are described in [Rate management inside Tesla charge windows](tesla-native-window-rate.md).

Every local battery lease uses fresh measured home load, including current EV consumption, to limit battery charging to available import headroom. Forecasts help plan future demand; they do not guarantee an unseen vehicle schedule or unreported load. Keep gateway site connection limits configured: software targets do not replace enforced electrical limits.

## Total grid import during Spread Import

From v2.12.1478, Spread Import levels total grid import across eligible windows while retaining planned battery watts for SOC and energy accounting. Paired LAN Powerwalls receive a separate grid-import target; each short lease uses fresh measured home/EV load and solar to calculate battery input. Missing forecast EV draw therefore leaves capacity available to the battery, and actual EV draw reduces battery input. Manual Charge input keeps its existing battery-power meaning. Other supported batteries receive their calculated battery targets; this does not establish the same local renewal cadence for every brand.

The command remains bounded by the configured charge rate, reported gateway charge ceiling and the tighter configured/gateway import limit. When home demand alone exceeds the target, the charging request falls to zero; it never silently becomes a discharge command. Loads that exceed a connection limit independently of battery charging cannot be fixed by a charge-only request. Keep gateway connection limits configured.

The optimiser uses reported charge/discharge ceilings and fresh gateway import limits without rewriting the saved battery rating. It replans from current SOC and remaining time, preserving its energy accounting after spreading. A capped command that cannot reach the grid target requests a throttled fresh solve. It cannot promise an energy target that the remaining time and available capacity make infeasible.

Battery Mode exposes `grid_import_target_w`, `power_target_basis`, `estimated_grid_import_w` and `grid_import_target_limited`. The estimate derives from the fresh pre-command load and acknowledged battery request; measured Grid Power remains the physical evidence. Optimisation status reports the planned grid target separately from planned battery watts and the acknowledged command.

## Tesla operating mode

Tesla defines `self_consumption` as Self-Powered and `autonomous` as Time-Based Control. The local timed-dispatch message contains battery watts and duration, or a resume request; it does not contain an operating-mode choice. PowerSync does not change the underlying Tesla mode to make a timed local command. Grid-charge permission, export rules, SOC protections and gateway power limits still apply. The legacy tariff path relies on autonomous/TOU behaviour, so its mode requirements must not be applied indiscriminately to native dispatch. Resume acknowledgement confirms the gateway accepted release; observe power and native mode readback to verify the resulting behaviour.

References: [Tesla energy operation endpoint](https://developer.tesla.com/docs/fleet-api/endpoints/energy), [aiopowerwall local protocol documentation](https://github.com/Teslemetry/aiopowerwall).


## Available grid-export targets

The Force Power export range respects both the configured site export limit and
the battery power available after current home load and solar. A gateway that
reports a 10 kW battery-output ceiling can provide about 2 kW of grid export
with 8 kW home load and no solar. A higher site export limit does not increase
the timed command's battery-output ceiling. The range refreshes with live data;
solar contribution can increase available export.

### Full-rate Max discharge

An explicit manual or automation **Max** discharge uses cloud tariff control when Tariff is selected. Its output is independent of the gateway’s learned timed-command ceiling. Numeric targets retain local wattage control and their live home/solar compensation. Max is not a promise to hold an exact grid-export target; installer site limits and reserves remain effective. When Automatic selects native tariff control, a stricter software export limit is temporarily applied and verified at the gateway. If that native takeover cannot be confirmed, it fails explicitly. The tariff, mode, reserve and export settings return through the existing restore lifecycle.
