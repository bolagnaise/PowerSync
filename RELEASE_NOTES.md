## What's Changed

**Fix: Tesla Grid Export Rule select no longer shows "unknown" on VPP / non-export sites**
When Tesla's `site_info` response omits `customer_preferred_export_rule` (which happens for VPP participants, non-export-configured sites, and other atypical setups), the `select.home_grid_export_rule` entity was returning `None` from `current_option` and HA displayed the entity as "unknown" on the dashboard. `PowerwallSettingsView` already had a fallback for this case — defaulting to `battery_ok` — but the select entity didn't. The select now:

1. Prefers `cached_export_rule` from `entry_data` if set (written whenever the user changes the rule via a service call, so survives API omissions)
2. Falls back to `components.customer_preferred_export_rule` from site_info
3. Falls back to `"never"` if `non_export_configured` is set
4. Falls back to `"battery_ok"` as a safe default so the entity is always selectable rather than reporting unknown

**Fix: Site-info cache invalidated after any Tesla Energy Site write**
When users changed backup reserve, operation mode, grid export rule, grid charging, storm watch, off-grid EV reserve, or VPP enrollment via a service call, the write succeeded against Tesla's API but PowerSync's 6-hour `_site_info_cache` was never invalidated. HA entities reading from the cached site_info — the number/select/switch/binary_sensor entities — kept showing the *old* value for up to six hours. One user reported their export rule showing as "PV ONLY" in the dashboard even though they'd just set it to `battery_ok`; this is the cause. The coordinator now exposes an `invalidate_site_info_cache()` method and every write path calls it after a successful POST, so the next read always re-fetches from Tesla.

**Fix: orphaned Tesla capability entities pruned after each probe**
If a user was briefly on a version (or Tesla API state) where the capability probe returned "supported" for a feature their site doesn't actually have — e.g. storm_watch on a non-US site that temporarily returned 200 for `/storm_mode` — the resulting `switch.home_storm_watch` / `binary_sensor.home_storm_watch_active` / `number.home_off_grid_ev_reserve` / VPP program switches persist in the entity registry and HA displays them as "unavailable" indefinitely. The capability probe now runs a cleanup pass after each run that removes registry entries for features the current site no longer supports. Combined with the strategy fix below, broken/phantom entities should disappear from the auto-generated dashboard on the next HA restart after upgrading.

**Fix: auto dashboard strategy filters out unavailable entities**
The `power-sync-strategy.js` entity finder used to fall through to the entity registry when a state lookup failed, which meant orphaned "unavailable" entities from prior versions still appeared in the "Tesla Energy Site" section as broken control tiles. The finder now requires entities to have a real available state before surfacing them, and the VPP switch scanner applies the same filter. Orphaned registry-only entries no longer pollute the dashboard.

**Fix: full baseline inheritance on force_charge ⇄ force_discharge transitions (rpcai PR #22 follow-up)**
The cross-state clearing fix in 2.11.7 prevented stale `active` flags between force modes, but each transition still overwrote the saved baseline (tariff / operation mode / backup reserve) with the intermediate force state — so when the second force mode auto-restored, it reverted to the first force state rather than the true pre-force baseline. Both transition paths now inherit the saved_* fields from the opposite state and skip re-saving, ensuring `force_charge → force_discharge → auto-restore` (or the reverse) returns the Powerwall to exactly the state it was in before *either* force command fired.

Update available via HACS
