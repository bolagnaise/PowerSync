<!-- release: v2.12.909 -->

## Tesla optimiser force charging

- Fixed repeated optimiser force-charge failures on Tesla Powerwalls whose
  Fleet API accepts grid charging but omits the grid-charging field from
  `site_info` readback.
- The optimiser now recognises this specific firmware/API response after
  repeated valid readbacks, allowing the charge tariff and Powerwall 3 charge
  kick to continue.
- Explicit contradictory values, malformed responses, failed or superseded
  commands, force discharge, restore, and manual controls remain strictly
  verified.

Update available via HACS.
