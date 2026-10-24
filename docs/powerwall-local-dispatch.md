# Paired Powerwall charge and export targets

Paired Powerwalls with a reachable LAN gateway use timed local battery control for manual charge/discharge and optimiser requests. Charge kW targets battery input. Discharge kW targets grid export, adjusting battery output for current home load and solar. Fresh telemetry, reserves, charging permission, configured power limits and gateway site limits still apply.

## Gateway command limits

The gateway can report a timed-command limit below the combined Powerwall rating. From v2.12.1477, PowerSync learns charge and discharge ceilings independently from an exact matching gateway rejection. It requests normal operation, requires the gateway’s acknowledgement, and permits one retry with fresh safety checks inside the original time window. Later commands and the manual power controls use that ceiling; the configured battery rating is unchanged. An unconfirmed resume blocks further dispatch. Tariff fallback cannot bypass a learned ceiling or enforce a selected kW target.

A four-Powerwall 2 site on firmware 26.34.0 reported a 10 kW timed charging ceiling despite its configured 20 kW combined rating. The reporter demonstrated 10 kW charging and 10 kW grid export. This is evidence for that system, not a universal Powerwall 2 limit or a guarantee for other firmware. Check measured battery and grid power; acknowledgement alone does not prove output.

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
