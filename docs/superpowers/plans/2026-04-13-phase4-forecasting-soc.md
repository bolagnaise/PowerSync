# Phase 4 — Smarter Forecasting + SOC Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give users max SOC control over the LP optimizer, track forecast accuracy with visible metrics, and auto-calibrate the load estimator's pattern weights from observed errors.

**Architecture:** Three features built in dependency order. SOC limit is independent (LP constraint + number entity). Forecast accuracy adds a comparison engine in the coordinator that logs forecast-vs-actual every 5 minutes and exposes error metrics as sensors. Auto-calibration hooks into that error data to adjust LoadEstimator pattern weights with exponential decay.

**Tech Stack:** Python 3.12 (HA custom integration), scipy LP (battery_optimizer), HA Store (persistence), voluptuous (config validation)

**Repo:** `~/Claude/energy/powersync/` — fork `Artic0din/PowerSync`, upstream `bolagnaise/PowerSync`

---

## File Map

| File | Responsibility | Tasks |
|------|---------------|-------|
| `optimization/battery_optimizer.py` | LP solver — SOC bounds | 1 |
| `optimization/coordinator.py` | Optimizer config, forecast tracking | 1, 2 |
| `number.py` | Max SOC number entity | 1 |
| `const.py` | Config keys, sensor types | 1, 2, 3 |
| `config_flow.py` | Max SOC in optimization options | 1 |
| `services.yaml` | set_max_soc service | 1 |
| `__init__.py` | Service handler registration | 1 |
| `optimization/load_estimator.py` | Pattern weights, calibration | 3 |
| `sensor.py` | Forecast accuracy sensors | 2 |

---

## Task 1: Max SOC Limit — LP constraint + number entity + config flow

**Files:**
- Modify: `optimization/battery_optimizer.py:75-100,412`
- Modify: `optimization/coordinator.py:48-58,238-256`
- Modify: `number.py:48-94`
- Modify: `const.py:596-608`
- Modify: `config_flow.py` (async_step_ml_options)
- Modify: `services.yaml`
- Modify: `__init__.py` (service registration)

**Problem:** LP upper SOC bound is hardcoded to `1.0` at `battery_optimizer.py:412`. Users cannot limit charge to less than 100%.

- [ ] **Step 1: Add max_soc to BatteryOptimizer**

In `optimization/battery_optimizer.py`, add `max_soc` parameter to `__init__` (after `backup_reserve`):

```python
def __init__(
    self,
    capacity_wh: float = 13500,
    max_charge_w: float = 5000,
    max_discharge_w: float = 5000,
    efficiency: float = DEFAULT_EFFICIENCY,
    backup_reserve: float = 0.20,
    max_soc: float = 1.0,
    hardware_reserve: float = 0.0,
    interval_minutes: int = 5,
    horizon_hours: int = 48,
):
    # ... existing assignments ...
    self.max_soc = max_soc
```

Then change the LP upper bound constraint (line ~412) from:
```python
b_ub.append(1.0 - soc_0)
```
to:
```python
b_ub.append(self.max_soc - soc_0)
```

Also update `update_config` if it exists — search for `def update_config` and add `max_soc` handling.

- [ ] **Step 2: Add max_soc to OptimizationConfig and coordinator**

In `optimization/coordinator.py`, add to `OptimizationConfig` dataclass:
```python
@dataclass
class OptimizationConfig:
    battery_capacity_wh: int = 13500
    max_charge_w: int = 5000
    max_discharge_w: int = 5000
    backup_reserve: float = 0.2
    max_soc: float = 1.0  # NEW — max charge SOC (0.0-1.0)
    interval_minutes: int = 5
    horizon_hours: int = 48
    cost_function: str = "cost"
```

In the optimizer initialization block (search for `BatteryOptimizer(`), add:
```python
self._optimizer = BatteryOptimizer(
    # ... existing params ...
    max_soc=self._config.max_soc,
    # ... rest ...
)
```

- [ ] **Step 3: Add config constant**

In `const.py`, add after `CONF_OPTIMIZATION_BACKUP_RESERVE`:
```python
CONF_OPTIMIZATION_MAX_SOC = "optimization_max_soc"
DEFAULT_OPTIMIZATION_MAX_SOC = 1.0  # 100%
```

- [ ] **Step 4: Add MaxSOCNumber entity**

In `number.py`, add after `BackupReserveNumber`:
```python
class MaxSOCNumber(_TeslaSiteNumberBase):
    """Maximum charge SOC % — LP optimizer will not charge above this level."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, entry,
            key="max_soc",
            name="Max Charge SOC",
            icon="mdi:battery-arrow-up",
        )
        self._attr_native_min_value = 50
        self._attr_native_max_value = 100
        self._attr_native_step = 5

    @property
    def native_value(self) -> float | None:
        stored = self._entry.options.get(CONF_OPTIMIZATION_MAX_SOC)
        if stored is not None:
            return float(stored) * 100  # stored as 0-1, display as %
        return 100.0

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN, "set_max_soc", {"percent": int(value)}, blocking=False,
        )
```

Add `MaxSOCNumber` to `async_setup_entry` in `number.py` — find where `BackupReserveNumber` is added and add:
```python
# Max SOC entity — available when optimization is enabled
from .const import CONF_OPTIMIZATION_ENABLED
if entry.options.get(CONF_OPTIMIZATION_ENABLED, False):
    async_add_entities([MaxSOCNumber(hass, entry)])
```

Import `CONF_OPTIMIZATION_MAX_SOC` at the top.

- [ ] **Step 5: Add set_max_soc service**

In `services.yaml`, add:
```yaml
set_max_soc:
  name: Set Max Charge SOC
  description: Set the maximum SOC the optimizer will charge to (50-100%).
  fields:
    percent:
      name: Percent
      description: Maximum charge SOC percentage
      required: true
      example: 90
      selector:
        number:
          min: 50
          max: 100
          step: 5
          unit_of_measurement: "%"
```

In `__init__.py`, find where `set_backup_reserve` service is registered (search for `SERVICE_SET_BACKUP_RESERVE` or `set_backup_reserve`). Add the `set_max_soc` handler nearby:

```python
async def handle_set_max_soc(call: ServiceCall) -> None:
    """Handle set_max_soc service call."""
    percent = call.data.get("percent", 100)
    max_soc = max(0.5, min(1.0, percent / 100.0))

    # Update config entry options
    for entry in hass.config_entries.async_entries(DOMAIN):
        new_options = {**entry.options, CONF_OPTIMIZATION_MAX_SOC: max_soc}
        hass.config_entries.async_update_entry(entry, options=new_options)

        # Update running optimizer if active
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        opt_coord = entry_data.get("optimization_coordinator")
        if opt_coord:
            opt_coord.update_config(max_soc=max_soc)

    _LOGGER.info("Max charge SOC set to %d%%", percent)

hass.services.async_register(DOMAIN, "set_max_soc", handle_set_max_soc)
```

- [ ] **Step 6: Add max_soc to config flow**

In `config_flow.py`, find `async_step_ml_options`. Add max_soc field to the schema:

```python
vol.Required(
    CONF_OPTIMIZATION_MAX_SOC,
    default=int(DEFAULT_OPTIMIZATION_MAX_SOC * 100)
): vol.All(vol.Coerce(int), vol.Range(min=50, max=100)),
```

And in the handler, save it:
```python
self._ml_options = {
    CONF_OPTIMIZATION_COST_FUNCTION: COST_FUNCTION_COST,
    CONF_OPTIMIZATION_BACKUP_RESERVE: user_input.get(
        CONF_OPTIMIZATION_BACKUP_RESERVE, int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE * 100)
    ) / 100.0,
    CONF_OPTIMIZATION_MAX_SOC: user_input.get(
        CONF_OPTIMIZATION_MAX_SOC, int(DEFAULT_OPTIMIZATION_MAX_SOC * 100)
    ) / 100.0,
}
```

- [ ] **Step 7: Validate and commit**

```bash
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['custom_components/power_sync/optimization/battery_optimizer.py', 'custom_components/power_sync/optimization/coordinator.py', 'custom_components/power_sync/number.py', 'custom_components/power_sync/const.py', 'custom_components/power_sync/config_flow.py', 'custom_components/power_sync/__init__.py']]; print('All OK')"
git add custom_components/power_sync/
git commit -m "feat(optimizer): add max SOC limit — LP constraint, number entity, config flow, service

- BatteryOptimizer accepts max_soc param, LP upper bound uses it instead of hardcoded 1.0
- MaxSOCNumber entity (slider 50-100%) for dashboard control
- set_max_soc service for automations
- Config flow step includes max SOC alongside backup reserve

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Forecast Accuracy Tracking — comparison engine + sensors

**Files:**
- Modify: `optimization/coordinator.py` (add accuracy tracking to _track_actual_cost)
- Modify: `const.py` (sensor type constants)
- Modify: `sensor.py` (accuracy sensors)

**Problem:** Zero tracking of forecast vs actual. No way to know if the optimizer is making good predictions.

- [ ] **Step 1: Add forecast accuracy state to coordinator**

In `optimization/coordinator.py`, add instance variables in `__init__` (near the cost tracking vars around line ~190):

```python
# Forecast accuracy tracking
self._forecast_errors: list[dict] = []  # Ring buffer of recent errors
self._forecast_error_max = 288  # 24h of 5-min intervals
self._last_forecast_comparison_time: datetime | None = None
```

- [ ] **Step 2: Add accuracy comparison in _track_actual_cost**

In `_track_actual_cost()` (the method that runs every 5 minutes), after the cost accumulation but before `self._schedule_cost_save()`, add:

```python
        # --- Forecast accuracy tracking ---
        if self._last_load_forecast and self._last_update_time:
            try:
                offset = self._get_forecast_offset()
                if offset < len(self._last_load_forecast):
                    forecast_load_kw = self._last_load_forecast[offset]
                    # Actual load = grid + solar + battery (power balance)
                    actual_load_kw = max(0.0, grid_power_kw + solar_power_kw + battery_power_kw)
                    error_kw = forecast_load_kw - actual_load_kw

                    self._forecast_errors.append({
                        "timestamp": now.isoformat(),
                        "forecast_kw": round(forecast_load_kw, 3),
                        "actual_kw": round(actual_load_kw, 3),
                        "error_kw": round(error_kw, 3),
                        "abs_error_kw": round(abs(error_kw), 3),
                    })
                    # Trim ring buffer
                    if len(self._forecast_errors) > self._forecast_error_max:
                        self._forecast_errors = self._forecast_errors[-self._forecast_error_max:]
            except Exception:
                pass  # Non-critical — don't break cost tracking
```

- [ ] **Step 3: Add accuracy metric methods**

Add these methods to `OptimizationCoordinator` (near `_get_daily_savings`):

```python
    def get_forecast_accuracy(self) -> dict:
        """Get forecast accuracy metrics from recent error history."""
        if not self._forecast_errors:
            return {
                "mae_kw": None,
                "rmse_kw": None,
                "bias_kw": None,
                "mape_percent": None,
                "samples": 0,
            }
        errors = self._forecast_errors
        n = len(errors)
        abs_errors = [e["abs_error_kw"] for e in errors]
        signed_errors = [e["error_kw"] for e in errors]
        actuals = [e["actual_kw"] for e in errors]

        mae = sum(abs_errors) / n
        rmse = (sum(e ** 2 for e in signed_errors) / n) ** 0.5
        bias = sum(signed_errors) / n  # positive = over-predicting

        # MAPE — skip intervals where actual is near zero (avoid div/0)
        mape_pairs = [(abs(e["error_kw"]), e["actual_kw"]) for e in errors if e["actual_kw"] > 0.1]
        mape = (sum(ae / a for ae, a in mape_pairs) / len(mape_pairs) * 100) if mape_pairs else None

        return {
            "mae_kw": round(mae, 3),
            "rmse_kw": round(rmse, 3),
            "bias_kw": round(bias, 3),
            "mape_percent": round(mape, 1) if mape is not None else None,
            "samples": n,
        }
```

- [ ] **Step 4: Expose accuracy in get_api_data**

In `get_api_data()`, add after the `savings_periods` block:
```python
        data["forecast_accuracy"] = self.get_forecast_accuracy()
```

- [ ] **Step 5: Add sensor constants**

In `const.py`, add:
```python
SENSOR_TYPE_FORECAST_MAE = "forecast_mae"
SENSOR_TYPE_FORECAST_BIAS = "forecast_bias"
SENSOR_TYPE_FORECAST_MAPE = "forecast_mape"
```

- [ ] **Step 6: Add accuracy sensors**

In `sensor.py`, add to the `SAVINGS_SENSORS` tuple (or create a new `FORECAST_ACCURACY_SENSORS` tuple):

```python
FORECAST_ACCURACY_SENSORS: tuple[PowerSyncSensorEntityDescription, ...] = (
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_FORECAST_MAE,
        name="Load Forecast MAE",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:chart-bell-curve",
        value_fn=lambda data: (
            data.get("forecast_accuracy", {}).get("mae_kw")
            if data else None
        ),
        attr_fn=lambda data: data.get("forecast_accuracy", {}) if data else {},
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_FORECAST_BIAS,
        name="Load Forecast Bias",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:arrow-up-down",
        value_fn=lambda data: (
            data.get("forecast_accuracy", {}).get("bias_kw")
            if data else None
        ),
    ),
    PowerSyncSensorEntityDescription(
        key=SENSOR_TYPE_FORECAST_MAPE,
        name="Load Forecast MAPE",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:percent-outline",
        value_fn=lambda data: (
            data.get("forecast_accuracy", {}).get("mape_percent")
            if data else None
        ),
    ),
)
```

Register these sensors alongside the savings sensors — find where `SAVINGS_SENSORS` is iterated in `async_setup_entry` and add:
```python
        for description in FORECAST_ACCURACY_SENSORS:
            entities.append(
                SavingsSensor(
                    coordinator=optimization_coordinator,
                    description=description,
                    entry=entry,
                )
            )
        _LOGGER.info("Forecast accuracy sensors added")
```

Import `SENSOR_TYPE_FORECAST_MAE`, `SENSOR_TYPE_FORECAST_BIAS`, `SENSOR_TYPE_FORECAST_MAPE` and `FORECAST_ACCURACY_SENSORS`.

- [ ] **Step 7: Validate and commit**

```bash
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['custom_components/power_sync/optimization/coordinator.py', 'custom_components/power_sync/const.py', 'custom_components/power_sync/sensor.py']]; print('All OK')"
git add custom_components/power_sync/
git commit -m "feat(forecast): add accuracy tracking — MAE, RMSE, bias, MAPE sensors

- Compare forecast vs actual load every 5 minutes in _track_actual_cost
- 288-entry ring buffer of error history (24h)
- Three new sensors: forecast_mae, forecast_bias, forecast_mape
- Accuracy metrics exposed in optimization API data

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Load Forecast Auto-Calibration — adaptive pattern weights

**Files:**
- Modify: `optimization/load_estimator.py` (calibration logic)
- Modify: `optimization/coordinator.py` (feed errors to estimator)

**Problem:** LoadEstimator pattern averages are computed fresh from 7-day history each time with no weighting. Recent forecast errors don't influence future predictions.

- [ ] **Step 1: Add calibration state to LoadEstimator**

In `optimization/load_estimator.py`, add to `LoadEstimator.__init__`:

```python
        # Calibration: per-pattern adjustment factors (multiplicative)
        # Key: (day_of_week, hour, half_hour), Value: adjustment multiplier (1.0 = no change)
        self._calibration_factors: dict[tuple[int, int, int], float] = {}
        self._calibration_decay = 0.9  # Exponential decay — recent errors weighted more
```

- [ ] **Step 2: Apply calibration in _forecast_from_history**

In `_forecast_from_history()`, find where the forecast value is looked up from `averages[key]`. After the value is determined but before it's appended to the result list, apply the calibration factor:

```python
            # Apply calibration adjustment if available
            cal_key = (dow, hour, half_hour)
            cal_factor = self._calibration_factors.get(cal_key, 1.0)
            value = value * cal_factor
```

This goes right before the value is appended to the forecast list.

- [ ] **Step 3: Add calibration update method**

Add to `LoadEstimator`:

```python
    def update_calibration(self, forecast_kw: float, actual_kw: float, timestamp: datetime) -> None:
        """Update calibration factors based on forecast vs actual comparison.

        Uses exponential moving average to adjust pattern weights.
        A forecast of 2.0 kW vs actual of 1.5 kW → factor moves toward 0.75.
        """
        if forecast_kw < 0.05 or actual_kw < 0.0:
            return  # Skip near-zero forecasts (avoid div/0, noise)

        dow = timestamp.weekday()
        hour = timestamp.hour
        half_hour = 1 if timestamp.minute >= 30 else 0
        key = (dow, hour, half_hour)

        # Target ratio: what the forecast should have been
        target_ratio = actual_kw / forecast_kw if forecast_kw > 0.05 else 1.0
        # Clamp to prevent extreme swings (0.5x to 2.0x)
        target_ratio = max(0.5, min(2.0, target_ratio))

        # Exponential moving average update
        current = self._calibration_factors.get(key, 1.0)
        alpha = 1.0 - self._calibration_decay  # 0.1 = 10% weight to new observation
        updated = current * self._calibration_decay + target_ratio * alpha
        self._calibration_factors[key] = round(updated, 4)
```

- [ ] **Step 4: Feed errors from coordinator to estimator**

In `optimization/coordinator.py`, in the forecast accuracy tracking block added in Task 2 (inside `_track_actual_cost`), add after the error is appended:

```python
                    # Feed to load estimator for auto-calibration
                    if self._load_estimator and hasattr(self._load_estimator, 'update_calibration'):
                        self._load_estimator.update_calibration(
                            forecast_kw=forecast_load_kw,
                            actual_kw=actual_load_kw,
                            timestamp=now,
                        )
```

- [ ] **Step 5: Persist calibration factors**

In `optimization/coordinator.py`, add calibration to the cost store persistence.

In `_cost_data_to_save()`, add:
```python
            "calibration_factors": {
                f"{k[0]},{k[1]},{k[2]}": v
                for k, v in self._load_estimator._calibration_factors.items()
            } if self._load_estimator and hasattr(self._load_estimator, '_calibration_factors') else {},
```

In `_restore_cost_data()`, add after history restoration:
```python
        # Restore calibration factors
        cal_data = data.get("calibration_factors", {})
        if cal_data and self._load_estimator and hasattr(self._load_estimator, '_calibration_factors'):
            for key_str, value in cal_data.items():
                try:
                    parts = key_str.split(",")
                    key = (int(parts[0]), int(parts[1]), int(parts[2]))
                    self._load_estimator._calibration_factors[key] = float(value)
                except (ValueError, IndexError):
                    pass
            if self._load_estimator._calibration_factors:
                _LOGGER.info(
                    "Restored %d calibration factors",
                    len(self._load_estimator._calibration_factors),
                )
```

- [ ] **Step 6: Validate and commit**

```bash
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['custom_components/power_sync/optimization/load_estimator.py', 'custom_components/power_sync/optimization/coordinator.py']]; print('All OK')"
git add custom_components/power_sync/
git commit -m "feat(forecast): add auto-calibration — adaptive pattern weights from observed errors

- Per-pattern (dow, hour, half_hour) calibration factors with exponential decay
- update_calibration() adjusts multiplicative weights (0.5x-2.0x clamp)
- Coordinator feeds forecast vs actual to estimator every 5 minutes
- Calibration factors persisted in cost store, restored on startup

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Verification

After all tasks complete:

```bash
for f in \
  custom_components/power_sync/optimization/battery_optimizer.py \
  custom_components/power_sync/optimization/coordinator.py \
  custom_components/power_sync/optimization/load_estimator.py \
  custom_components/power_sync/number.py \
  custom_components/power_sync/const.py \
  custom_components/power_sync/config_flow.py \
  custom_components/power_sync/sensor.py \
  custom_components/power_sync/__init__.py; do
  python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" || echo "FAIL: $f"
done
```

Update `CHANGELOG.md` under `[Unreleased]`:
```markdown
### Added
- Max SOC limit: number entity (50-100%), LP optimizer constraint, set_max_soc service, config flow
- Forecast accuracy: MAE, bias, MAPE sensors from 24h error ring buffer
- Load forecast auto-calibration: adaptive pattern weights with exponential decay, persisted across restarts
```

Update `IMPROVEMENTS.md` Phase 4 status from `PLANNED` to `COMPLETED`.
