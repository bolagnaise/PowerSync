<!-- release: v2.12.1809 -->

## Tesla grid-charge recovery

PowerSync now checks how recently Tesla power was measured before trying to restore normal operation during self-consumption. This prevents an old grid-import reading from triggering a tariff and settings restore after the battery has switched to solar charging.

Update PowerSync through HACS and restart Home Assistant.
