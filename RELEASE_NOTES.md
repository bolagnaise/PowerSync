<!-- release: v2.12.773 -->

## Fixes

- Fixed Sungrow SH telemetry during nighttime forced-discharge/export windows where some SH10RS/WiNet-S systems report battery discharge power through the PV DC register. PowerSync now detects that aliasing case at night, reports solar as zero, and derives home load from battery discharge minus grid export instead of inflating both solar and home load by the discharge power.

## Verification

- Added a Sungrow coordinator regression test using the reported 0 W solar / 10 kW discharge / 9.5 kW export case.
- Ran `python3.12 -m pytest -q tests/test_sungrow_sh_controller.py tests/test_sungrow_curtailment_runtime.py`.
