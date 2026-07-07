<!-- release: v2.12.782 -->

## Fixes

- Fixed priority export planning for ZeroHero and other export windows so the
  post-processed reserve floor is calculated after the whole contiguous export
  run, rather than from each individual export slot. This prevents forecast
  home load inside the export window from being double-counted as reserve that
  must be kept back, allowing the optimiser to export closer to the configured
  reserve before the next planned charge opportunity.

## Verification

- `python3.12 -m pytest -q tests/test_battery_export_allowed_slots.py::test_export_reserve_floor_bridges_after_contiguous_export_run tests/test_battery_export_allowed_slots.py::test_spread_export_schedule_respects_auto_reserve_export_floor tests/test_battery_export_allowed_slots.py::test_spread_export_schedule_carries_reserve_soc_after_capped_export`
- `python3.12 -m pytest -q tests/test_battery_export_allowed_slots.py tests/test_battery_optimizer_export_guard.py tests/test_zerohero_settlement.py`
- `python3.12 -m py_compile custom_components/power_sync/optimization/coordinator.py`
