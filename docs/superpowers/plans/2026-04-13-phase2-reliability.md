# Phase 2 — Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bugs that erode user confidence — stable dashboard, smooth config, reliable battery control, accurate EV stats.

**Architecture:** Four independent fix groups targeting the highest-severity bugs found in audit. Each group produces a separate commit. No new features — only fixes to existing behavior. All changes are in the PowerSync HACS integration at `custom_components/power_sync/`.

**Tech Stack:** Python 3.12 (HA custom integration), JavaScript (Lovelace cards), voluptuous (schema validation), Home Assistant helpers (Store, events, services)

**Repo:** `~/Claude/energy/powersync/` — fork `Artic0din/PowerSync`, upstream `bolagnaise/PowerSync`

---

## File Map

| File | Responsibility | Tasks |
|------|---------------|-------|
| `frontend/power-sync-strategy.js` | Dashboard card generation | 1, 2 |
| `config_flow.py` | Setup & options UI | 3 |
| `inverters/goodwe_battery.py` | GoodWe force charge/discharge | 4 |
| `inverters/foxess.py` | FoxESS force charge/discharge | 5 |
| `inverters/sigenergy.py` | Sigenergy force charge/discharge | 6 |
| `optimization/coordinator.py` | Optimizer action execution | 7 |
| `automations/ev_charging_session.py` | EV session energy tracking | 8 |
| `__init__.py` | Zaptec EV integration, force mode persistence | 9 |

---

## Group A: Dashboard Stability

### Task 1: Fix entity resolver to use config prefix and support all domains

**Files:**
- Modify: `frontend/power-sync-strategy.js:498-505`

**Problem:** The entity resolver `e()` is hardcoded to `sensor.power_sync_` prefix and only checks `sensor.` domain. The `entity_prefix` config option is parsed but never used. Battery controls at lines 844-898 bypass `e()` entirely with hardcoded entity names.

- [ ] **Step 1: Fix `e()` to use config prefix and check multiple domains**

Replace lines 498-505:
```javascript
const cfgPrefix = config.entity_prefix || 'power_sync';
const e = (name, domain = 'sensor') => {
  // Try configured prefix first
  const prefixed = `${domain}.${cfgPrefix}_${name}`;
  if (hass.states[prefixed]) return prefixed;
  // Try power_sync_ if different from configured
  if (cfgPrefix !== 'power_sync') {
    const ps = `${domain}.power_sync_${name}`;
    if (hass.states[ps]) return ps;
  }
  // Try bare name
  const bare = `${domain}.${name}`;
  if (hass.states[bare]) return bare;
  // Default to configured prefix
  return prefixed;
};
```

- [ ] **Step 2: Fix battery controls to use `e()` resolver**

Replace the hardcoded entity references at lines 844-898. Change every occurrence of:
```javascript
entity: 'select.power_sync_force_charge_duration',
```
to:
```javascript
entity: e('force_charge_duration', 'select'),
```

And every template reference:
```javascript
states['select.power_sync_force_charge_duration']
```
to use a variable defined at the top of `_batteryControls()`:
```javascript
const chargeDurEntity = e('force_charge_duration', 'select');
const dischargeDurEntity = e('force_discharge_duration', 'select');
```
Then in templates:
```javascript
name: `[[[ return (states['${chargeDurEntity}'] ? states['${chargeDurEntity}'].state : '30') + ' min' ]]]`,
```

Apply the same pattern to all 8 hardcoded references (lines 844, 848, 861, 864, 870, 874, 895, 898).

- [ ] **Step 3: Verify no remaining hardcoded `power_sync_` outside `e()` default**

```bash
grep -n "power_sync_" custom_components/power_sync/frontend/power-sync-strategy.js | grep -v "cfgPrefix\|'power_sync'" | grep -v "^[[:space:]]*\/\/"
```

Expected: Only the fallback inside `e()` and comments.

- [ ] **Step 4: Validate JS syntax**

```bash
node --check custom_components/power_sync/frontend/power-sync-strategy.js && echo "OK"
```

- [ ] **Step 5: Commit**

```bash
git add custom_components/power_sync/frontend/power-sync-strategy.js
git commit -m "fix(dashboard): use config prefix in entity resolver, fix hardcoded battery controls"
```

---

### Task 2: Add dashboard-level error boundary and FoxESS existence checks

**Files:**
- Modify: `frontend/power-sync-strategy.js:456-754` (generate function), `1740-1758` (FoxESS sensors)

**Problem:** A single card builder exception crashes the entire dashboard. FoxESS sensors are always added without checking entity existence.

- [ ] **Step 1: Wrap card builder calls in try-catch**

In the `generate()` function, find the main card-building block (lines ~560-750 where `left`, `center`, `right` arrays are populated). Wrap each section in try-catch:

```javascript
// Energy flow
try {
  if (hasTeslaFlow) {
    center.push(_teslaStyleFlow(e, has));
  }
} catch (err) {
  console.error('PowerSync: Failed to build energy flow card:', err);
  center.push({ type: 'markdown', content: `⚠️ Energy flow card error: ${err.message}` });
}

// Battery controls
try {
  center.push(_batteryControls(e, has));
} catch (err) {
  console.error('PowerSync: Failed to build battery controls:', err);
  center.push({ type: 'markdown', content: `⚠️ Battery controls error: ${err.message}` });
}
```

Apply the same pattern to every card builder call (`_touSchedule`, `_lpForecastSummary`, `_lpPriceChart`, `_lpSolarLoadChart`, `_batteryHealth`, `_foxessSensors`, `_acInverterControls`).

- [ ] **Step 2: Fix FoxESS sensors — check entity existence before adding**

In `_foxessSensors()` (line ~1740), replace unconditional pushes with existence checks:

```javascript
function _foxessSensors(e, has) {
  const entities = [];
  const maybeAdd = (key, name) => {
    const id = e(key);
    if (has(id)) entities.push({ entity: id, name });
  };
  maybeAdd('pv1_power', 'PV1 Power');
  maybeAdd('pv2_power', 'PV2 Power');
  maybeAdd('ct2_power', 'CT2 Power');
  maybeAdd('work_mode', 'Work Mode');
  maybeAdd('min_soc', 'Min SOC');
  maybeAdd('daily_battery_charge_foxess', 'Daily Charge');
  maybeAdd('daily_battery_discharge_foxess', 'Daily Discharge');
  if (entities.length === 0) return null;
  return {
    type: 'entities',
    title: 'FoxESS Details',
    entities,
  };
}
```

Update the caller to check for null:
```javascript
const foxCard = _foxessSensors(e, has);
if (foxCard) right.push(foxCard);
```

- [ ] **Step 3: Validate and commit**

```bash
node --check custom_components/power_sync/frontend/power-sync-strategy.js && echo "OK"
git add custom_components/power_sync/frontend/power-sync-strategy.js
git commit -m "fix(dashboard): add error boundaries, check FoxESS entity existence"
```

---

## Group B: Config Flow Fixes

### Task 3: Fix critical options save bug and add validation

**Files:**
- Modify: `config_flow.py:4557` (options save), `config_flow.py:5692` (tariff default)

**Problem:** `_save_ev_options()` calls `self.async_create_entry(title="", data=final_data)` which creates a new config entry instead of updating options. Also, "add another tariff" defaults to True.

- [ ] **Step 1: Verify the bug exists in current code**

```bash
grep -n "async_create_entry" custom_components/power_sync/config_flow.py | head -20
```

Look for the call inside `_save_ev_options` that should be `async_create_entry` (the OptionsFlow version saves to options, but the method name is the same — verify the class context).

Note: In HA OptionsFlow, `self.async_create_entry(title="", data=...)` is actually correct — it saves to options, not a new entry. **Verify this is actually a bug before changing.** Read the HA developer docs pattern:

```python
# OptionsFlow.async_create_entry saves to options, NOT a new config entry
# This is different from ConfigFlow.async_create_entry
```

If this is NOT a bug (OptionsFlow uses same method name), mark as false positive and skip.

- [ ] **Step 2: Fix "add another tariff" default**

At line 5692, change:
```python
vol.Optional("add_another", default=True): bool,
```
to:
```python
vol.Optional("add_another", default=False): bool,
```

- [ ] **Step 3: Add Modbus port range validation**

Find Modbus port fields (search for `CONF_SIGENERGY_MODBUS_PORT`). Add range validation:

```python
vol.Optional(
    CONF_SIGENERGY_MODBUS_PORT,
    default=DEFAULT_SIGENERGY_MODBUS_PORT,
): vol.All(int, vol.Range(min=1, max=65535)),
vol.Optional(
    CONF_SIGENERGY_MODBUS_SLAVE_ID,
    default=DEFAULT_SIGENERGY_MODBUS_SLAVE_ID,
): vol.All(int, vol.Range(min=0, max=247)),
```

- [ ] **Step 4: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/config_flow.py').read()); print('OK')"
git add custom_components/power_sync/config_flow.py
git commit -m "fix(config): tariff default, Modbus port validation"
```

---

## Group C: Force Discharge Reliability

### Task 4: Fix GoodWe — check return values from set_operation_mode

**Files:**
- Modify: `inverters/goodwe_battery.py:85-111`

**Problem:** `force_charge()` and `force_discharge()` always return `True` regardless of whether `set_operation_mode()` succeeds or throws.

- [ ] **Step 1: Add try-except and return value propagation**

Replace lines 85-111:

```python
async def force_charge(self, power_pct: int = 100, soc_target: int = 100) -> bool:
    """Force charge from grid using ECO_CHARGE mode."""
    import goodwe

    try:
        await self._inverter.set_operation_mode(
            goodwe.OperationMode.ECO_CHARGE,
            eco_mode_power=power_pct,
            eco_mode_soc=soc_target,
        )
        _LOGGER.info(
            "GoodWe force charge: power=%d%%, target_soc=%d%%", power_pct, soc_target
        )
        return True
    except Exception as e:
        _LOGGER.error("GoodWe force charge failed: %s", e)
        return False

async def force_discharge(self, power_pct: int = 100, soc_floor: int = 10) -> bool:
    """Force discharge to grid using ECO_DISCHARGE mode."""
    import goodwe

    try:
        await self._inverter.set_operation_mode(
            goodwe.OperationMode.ECO_DISCHARGE,
            eco_mode_power=power_pct,
            eco_mode_soc=soc_floor,
        )
        _LOGGER.info(
            "GoodWe force discharge: power=%d%%, floor_soc=%d%%", power_pct, soc_floor
        )
        return True
    except Exception as e:
        _LOGGER.error("GoodWe force discharge failed: %s", e)
        return False
```

- [ ] **Step 2: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/inverters/goodwe_battery.py').read()); print('OK')"
git add custom_components/power_sync/inverters/goodwe_battery.py
git commit -m "fix(goodwe): propagate force charge/discharge errors instead of always returning True"
```

---

### Task 5: Fix FoxESS — return False when verify fails after retries

**Files:**
- Modify: `inverters/foxess.py:954-973`

**Problem:** After max retry attempts, verify mismatch logs a warning but falls through and returns `True`.

- [ ] **Step 1: Add explicit return False after retries exhausted**

At line 973, after the final warning, add:
```python
                            _LOGGER.warning(
                                "FoxESS %s power verify still mismatched after %d attempts — "
                                "inverter may not have actioned the command",
                                label,
                                max_attempts,
                            )
                            return False  # Don't mask the failure
```

Search the file for similar patterns where verify failures fall through. Check the `_verify_work_mode` method too.

- [ ] **Step 2: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/inverters/foxess.py').read()); print('OK')"
git add custom_components/power_sync/inverters/foxess.py
git commit -m "fix(foxess): return False when power verify fails after retries"
```

---

### Task 6: Fix Sigenergy — propagate power target failure

**Files:**
- Modify: `inverters/sigenergy.py:1291-1306`

**Problem:** Power target write failure only warns and continues, executing an incomplete force discharge.

- [ ] **Step 1: Return False when power target write fails**

Find the block around line 1303:
```python
power_result = await self._write_holding_registers(...)
if not power_result:
    _LOGGER.warning(f"Failed to set active power target to {-power_kw} kW, falling back to export limit only")
```

Change to:
```python
power_result = await self._write_holding_registers(...)
if not power_result:
    _LOGGER.error(
        "Failed to set active power target to %s kW — aborting force discharge",
        -power_kw,
    )
    return False
```

- [ ] **Step 2: Fix restore_export_limit failure in restore_normal**

Find the block around line 1400:
```python
export_result = await self.restore_export_limit()
if not export_result:
    _LOGGER.warning("Failed to restore grid export limit")
```

Change to:
```python
export_result = await self.restore_export_limit()
if not export_result:
    _LOGGER.error("Failed to restore grid export limit — system may have incorrect export settings")
    return False
```

- [ ] **Step 3: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/inverters/sigenergy.py').read()); print('OK')"
git add custom_components/power_sync/inverters/sigenergy.py
git commit -m "fix(sigenergy): propagate force discharge and restore failures"
```

---

### Task 7: Fix optimizer — propagate hardware extension failures

**Files:**
- Modify: `optimization/coordinator.py:1035-1036`

**Problem:** When the optimizer re-issues a Modbus command for hardware timer extension and it fails, the warning is logged but the software expiry timer is still extended, creating a mismatch.

- [ ] **Step 1: Don't extend expiry if Modbus re-issue failed**

Find the block around line 1035:
```python
                        except Exception as ext_err:
                            _LOGGER.warning(
                                "Optimizer: failed to re-issue Modbus %s for extension: %s",
                                force_type,
                                ext_err,
                            )
```

Add an early return or skip the timer extension:
```python
                        except Exception as ext_err:
                            _LOGGER.error(
                                "Optimizer: failed to re-issue Modbus %s for extension: %s — "
                                "NOT extending software timer (hardware will expire naturally)",
                                force_type,
                                ext_err,
                            )
                            # Don't extend expiry timer — let hardware timeout trigger restore
                            return
```

- [ ] **Step 2: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/optimization/coordinator.py').read()); print('OK')"
git add custom_components/power_sync/optimization/coordinator.py
git commit -m "fix(optimizer): don't extend software timer when hardware re-issue fails"
```

---

## Group D: EV Charging Stats

### Task 8: Fix negative energy, ghost sessions, silent failures

**Files:**
- Modify: `automations/ev_charging_session.py:216-227` (negative energy), `455-457` (silent failure), add timeout logic

**Problem:** Clock skew produces negative `interval_seconds`, which produces negative energy and costs. Sessions never time out. Updates silently fail when session is missing.

- [ ] **Step 1: Floor interval_seconds at 0**

At line 218, change:
```python
                interval_seconds = min(interval_seconds, 120.0)
```
to:
```python
                interval_seconds = max(0.0, min(interval_seconds, 120.0))
```

And at line 227, add a guard:
```python
        # Calculate energy for this interval
        if interval_seconds <= 0:
            _LOGGER.debug("Skipping zero/negative interval (%.1fs)", interval_seconds)
            return
        energy_kwh = (power_kw * interval_seconds) / 3600
```

- [ ] **Step 2: Log warning when session missing in update_session**

At line 455-457, change:
```python
        session = self.active_sessions.get(vehicle_id)
        if not session:
            return None
```
to:
```python
        session = self.active_sessions.get(vehicle_id)
        if not session:
            _LOGGER.warning(
                "EV session update for %s but no active session — readings lost. "
                "Call start_session() first.",
                vehicle_id,
            )
            return None
```

- [ ] **Step 3: Add session timeout (30 minutes of no updates)**

Add a method to `ChargingSessionManager`:
```python
    def cleanup_stale_sessions(self, timeout_minutes: int = 30) -> list[str]:
        """End sessions that haven't received an update in timeout_minutes.

        Returns list of vehicle_ids that were cleaned up.
        """
        now = datetime.now()
        stale = []
        for vehicle_id, session in list(self.active_sessions.items()):
            if session.last_reading_time:
                try:
                    last = datetime.fromisoformat(session.last_reading_time)
                    if (now - last).total_seconds() > timeout_minutes * 60:
                        stale.append(vehicle_id)
                except (ValueError, TypeError):
                    pass
        for vehicle_id in stale:
            _LOGGER.warning(
                "EV session for %s stale (>%d min since last update) — auto-ending",
                vehicle_id,
                timeout_minutes,
            )
            self.end_session(vehicle_id)
        return stale
```

- [ ] **Step 4: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/automations/ev_charging_session.py').read()); print('OK')"
git add custom_components/power_sync/automations/ev_charging_session.py
git commit -m "fix(ev): floor negative intervals, warn on missing session, add stale session cleanup"
```

---

### Task 9: Fix Zaptec hardcoded is_solar=False

**Files:**
- Modify: `__init__.py:20074`

**Problem:** All Zaptec session updates hardcode `is_solar=False`, so solar charging is never attributed correctly.

- [ ] **Step 1: Find and fix the hardcoded is_solar**

Search for the Zaptec session update:
```bash
grep -n "is_solar=False" custom_components/power_sync/__init__.py | head -10
```

Replace the hardcoded `is_solar=False` with actual solar detection. Use the same pattern as `actions.py:2577` — check if grid import is less than 20% of charger power:

```python
# Detect solar: if grid import < 20% of charger power, consider it solar
grid_power_kw = 0.0
if energy_coordinator and energy_coordinator.data:
    grid_power_kw = float(energy_coordinator.data.get("grid_power", 0) or 0)
charger_power_kw = total_charge_power_w / 1000
is_solar = grid_power_kw < (charger_power_kw * 0.2) if charger_power_kw > 0.1 else False
```

Replace the hardcoded call:
```python
session_mgr.update_session(
    vehicle_id=vehicle_id,
    power_kw=charger_power_kw,
    amps=current_amps,
    is_solar=is_solar,  # was: is_solar=False
    import_price_cents=import_price,
    export_price_cents=export_price,
)
```

- [ ] **Step 2: Validate and commit**

```bash
python3 -c "import ast; ast.parse(open('custom_components/power_sync/__init__.py').read()); print('OK')"
git add custom_components/power_sync/__init__.py
git commit -m "fix(ev): detect solar for Zaptec sessions instead of hardcoding grid"
```

---

## Verification

After all tasks are complete:

```bash
# Syntax check all modified files
for f in \
  custom_components/power_sync/frontend/power-sync-strategy.js \
  custom_components/power_sync/config_flow.py \
  custom_components/power_sync/inverters/goodwe_battery.py \
  custom_components/power_sync/inverters/foxess.py \
  custom_components/power_sync/inverters/sigenergy.py \
  custom_components/power_sync/optimization/coordinator.py \
  custom_components/power_sync/automations/ev_charging_session.py \
  custom_components/power_sync/__init__.py; do
  if [[ $f == *.js ]]; then
    node --check "$f" && echo "OK: $f" || echo "FAIL: $f"
  else
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" || echo "FAIL: $f"
  fi
done
```

Update `CHANGELOG.md` under `[Unreleased]`:
```markdown
### Fixed
- Dashboard: entity resolver uses config prefix, battery controls use resolver instead of hardcoded names
- Dashboard: error boundaries prevent single card crash from breaking entire dashboard
- Dashboard: FoxESS sensors check entity existence before rendering
- Config: "add another tariff" defaults to unchecked, Modbus port range validation
- Force discharge: GoodWe propagates errors instead of always returning True
- Force discharge: FoxESS returns False when power verify fails after retries
- Force discharge: Sigenergy aborts force discharge on power target write failure
- Optimizer: software timer not extended when hardware Modbus re-issue fails
- EV: negative energy from clock skew floored at zero
- EV: warning logged when session update called without active session
- EV: stale sessions auto-cleaned after 30 minutes of inactivity
- EV: Zaptec solar detection replaces hardcoded grid-only attribution
```

Update `IMPROVEMENTS.md` Phase 2 status from `PLANNED` to `IN PROGRESS`.
