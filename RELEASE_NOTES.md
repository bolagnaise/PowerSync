<!-- release: v2.12.1334 -->

## What's Changed

- Fix OCPP EV current control with HACS OCPP 0.12 and newer by using the
  connector's transaction-bound Session Current Limit API instead of the
  station-wide maximum API.
- Refuse a managed OCPP start when a transaction-bound current limit is not
  available yet, preventing an uncontrolled start and avoiding invalid
  `ChargingStationMaxProfile` requests.
- Keep the legacy HACS station-maximum fallback for older OCPP integrations
  that do not expose transaction current control.

Update available via HACS.
