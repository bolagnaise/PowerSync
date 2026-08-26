<!-- release: v2.12.1198 -->

## Fixed

- Coalesce AC-inverter status polling so slow Enphase Envoy responses cannot
  overlap scheduled, curtailment-dispatcher, and mobile status refreshes.
- Keep optimizer-owned battery force commands from refreshing hardware while a
  Powerwall calibration alert is active.
