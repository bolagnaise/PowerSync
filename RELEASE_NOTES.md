<!-- release: v2.12.1199 -->

## Fixed

- Restore PowerSync as the default authority for externally started EV
  charging. Enabled Smart Schedule, Scheduled Charging, Price-Level, and Solar
  Surplus modes can again stop an ineligible session or manage its charging
  current through their normal control paths.
- Add an explicit per-vehicle **Allow hands-off external charging** opt-in. When
  selected, PowerSync automated modes do not stop that externally started
  session or adjust its amps until unplug; Manual and Boost controls can still
  take over.
- Keep external-control ownership VIN/loadpoint-scoped across Tesla Fleet and
  BLE identity changes, preserve delayed stop-readback settling, and reject
  ambiguous Tesla default profiles instead of applying a policy to the wrong
  vehicle.
- Expose normalized policy and capability state through the EV configuration
  and loadpoint status APIs. Policy changes release only software ownership and
  do not issue vehicle or charger commands from the settings request.
- PowerSync Mobile build 421 adds the per-vehicle control selector, old-HA
  capability handling, save confirmation, and a clear warning that Tesla app
  starts and automatic start-on-plug cannot be distinguished.
