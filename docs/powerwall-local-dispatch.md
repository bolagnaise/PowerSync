# Paired Powerwall charge and export targets

Paired Powerwalls with a reachable LAN gateway use timed local battery control for manual charge/discharge and optimiser requests. Charge kW targets battery input. Discharge kW targets grid export, adjusting battery output for current home load and solar. Fresh telemetry, reserves, charging permission, configured power limits and gateway site limits still apply.

## Gateway command limits

The gateway can report a timed-command limit below the combined Powerwall rating. From v2.12.1477, PowerSync learns charge and discharge ceilings independently from an exact matching gateway rejection. It requests normal operation, requires the gateway’s acknowledgement, and permits one retry with fresh safety checks inside the original time window. Later commands and the manual power controls use that ceiling; the configured battery rating is unchanged. An unconfirmed resume blocks further dispatch. Tariff fallback cannot bypass a learned ceiling or enforce a selected kW target.

A four-Powerwall 2 site on firmware 26.34.0 reported a 10 kW timed charging ceiling despite its configured 20 kW combined rating. The reporter demonstrated 10 kW charging and 10 kW grid export. This is evidence for that system, not a universal Powerwall 2 limit or a guarantee for other firmware. Check measured battery and grid power; acknowledgement alone does not prove output.

## Native Max charging (under validation)

The next Home Assistant change distinguishes **Max** from a numeric battery-input target. For a manual or automation `force_charge` action, zero or omitted `power_w` requests Tesla-managed maximum charging. An eligible paired system resumes local dispatch, temporarily uses a charging tariff and Time-Based Control, and restores its original tariff, mode and reserve afterwards. No extra enable switch is needed. Max can exceed the optimiser's configured charge planning rate; numeric targets, optimiser commands and Spread Import keep their local wattage controls.

Grid-charging permission, SOC protection, demand protection, Monitoring Mode and other controller ownership still apply. A finite PowerSync import limit permits native Max only when a fresh gateway import limit is no higher. Active or unsettled EV control keeps charging on the local path, where prospective EV demand can be reserved. Max therefore does not bypass a lower software import limit or an EV reservation.

The timed-command ceiling is separate from native charging capability. An owner-authorized PW3 test reached 13.82 kW battery charging with 0.94 kW home load and 14.76 kW grid import, retaining the site's 14.9 kW import limit. The only observed `config.json` changes were mode and reserve; there was no adjustable 10 kW dispatch-limit setting. This demonstrates charging above 10 kW on that site, rather than a universal charge rating.

Native TOU control can take roughly a minute to affect measured power, including after restoring settings. Acknowledgement or restored settings do not prove charging has physically stopped. Keep checking Battery Power and Grid Power. This path relies on Home Assistant for timed restoration; it does not have the short device-side lease used by numeric local dispatch. Its restore journal survives restart, and uncertain cleanup blocks a new control owner. Manual changes are preserved while PowerSync removes the remaining settings it owns.

After restoring settings, PowerSync retains its charging reservation until two fresh, balanced readings show charging has returned to its pre-Max level, allowing a small measurement tolerance. Restart repeats those observations. This prevents an EV controller from reusing capacity while the additional Max charging is still settling.

A local HTTP timeout does not expire a signed gateway command. PowerSync records the actual signature deadline before sending a configuration write and closes that acceptance window before reconciling settings. An uncertain Fleet tariff upload has no equivalent known expiry: it remains pending until its submitted tariff can be reconciled. Elapsed time or an unchanged tariff alone does not authorize a competing controller.

This section describes the unreleased Home Assistant candidate. Cloud Max charging has a separate implementation and release history; its publication does not establish installation or verification of this HA change.

## Full battery and cached charging plans

From v2.12.1479, the optimiser checks fresh paired-gateway SOC before applying or extending a charge. At the configured charge ceiling, including 100%, it restores normal operation once and suppresses repeated cached charge attempts. It can resume charging when a later fresh reading is below the ceiling. Stale, failed or invalid local readings do not establish that the battery is full. The local command retains its independent safety check.

Optimiser charge logs label the requested watts explicitly. The local acknowledgement reports the accepted battery target separately; measured battery/grid power establishes the physical result. A 20 kW request followed by a 10 kW acknowledgement is not proof of 20 kW charging.

## Spread Import, Spread Export and EV charging

Home Assistant's Spread Import distributes battery charging across eligible same-price windows. Spread Export distributes planned export across eligible windows. They retain reserve, price, ownership and connection limits; Cloud does not expose these HA switches.

The optimiser already plans PowerSync-managed EV charging alongside the battery and treats supported native Tesla charge windows as fixed forecast demand without taking vehicle control. Spread Import now reserves the managed EV's planned power when redistributing battery energy, in addition to native Tesla demand already included in the load forecast. These sources are counted separately, once each.

An optional native-window vehicle rate controller is being validated separately. Its default-off permission, shared charging budget and restoration rules are described in [Rate management inside Tesla charge windows](tesla-native-window-rate.md). It has not been released with the existing battery dispatch changes.

Every local battery lease uses fresh measured home load, including current EV consumption, to limit battery charging to available import headroom. Forecasts help plan future demand; they do not guarantee an unseen vehicle schedule or unreported load. Keep gateway site connection limits configured: software targets do not replace enforced electrical limits.

## Total grid import during Spread Import

From v2.12.1478, Spread Import levels total grid import across eligible windows while retaining planned battery watts for SOC and energy accounting. Paired LAN Powerwalls receive a separate grid-import target; each short lease uses fresh measured home/EV load and solar to calculate battery input. Missing forecast EV draw therefore leaves capacity available to the battery, and actual EV draw reduces battery input. Manual Charge input keeps its existing battery-power meaning. Other supported batteries receive their calculated battery targets; this does not establish the same local renewal cadence for every brand.

The command remains bounded by the configured charge rate, reported gateway charge ceiling and the tighter configured/gateway import limit. When home demand alone exceeds the target, the charging request falls to zero; it never silently becomes a discharge command. Loads that exceed a connection limit independently of battery charging cannot be fixed by a charge-only request. Keep gateway connection limits configured.

The optimiser uses reported charge/discharge ceilings and fresh gateway import limits without rewriting the saved battery rating. It replans from current SOC and remaining time, preserving its energy accounting after spreading. A capped command that cannot reach the grid target requests a throttled fresh solve. It cannot promise an energy target that the remaining time and available capacity make infeasible.

Battery Mode exposes `grid_import_target_w`, `power_target_basis`, `estimated_grid_import_w` and `grid_import_target_limited`. The estimate derives from the fresh pre-command load and acknowledged battery request; measured Grid Power remains the physical evidence. Optimisation status reports the planned grid target separately from planned battery watts and the acknowledged command.

## Tesla operating mode

Tesla defines `self_consumption` as Self-Powered and `autonomous` as Time-Based Control. The local timed-dispatch message contains battery watts and duration, or a resume request; it does not contain an operating-mode choice. PowerSync does not change the underlying Tesla mode to make a timed local command. Grid-charge permission, export rules, SOC protections and gateway power limits still apply. The legacy tariff path relies on autonomous/TOU behaviour, so its mode requirements must not be applied indiscriminately to native dispatch. Resume acknowledgement confirms the gateway accepted release; observe power and native mode readback to verify the resulting behaviour.

References: [Tesla energy operation endpoint](https://developer.tesla.com/docs/fleet-api/endpoints/energy), [aiopowerwall local protocol documentation](https://github.com/Teslemetry/aiopowerwall).
