<!-- release: v2.12.1485 -->
## Tesla Grid Charging status

PowerSync now recovers automatically after a temporary Tesla or Teslemetry Site Info failure, rather than leaving the Grid Charging switch stuck until restart. Failed cloud refreshes show Unknown when fresh local permission data is unavailable, and retry after a five-minute cooldown.

Grid Charging reports Tesla's permission, not whether the battery is charging now. Update through HACS and restart Home Assistant.
