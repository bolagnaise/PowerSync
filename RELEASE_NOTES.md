<!-- release: v2.12.1539 -->

## What's Changed

**Combined SolarEdge AC output**
The AC Inverter card can now total leader and follower output. In AC Inverter settings, select each inverter's local AC sensor under **SolarEdge AC power sensors (optional)**, including the leader once.

Missing or older readings show **Telemetry incomplete** until every selected sensor reports within two minutes. This selection changes the displayed power; inverter controls retain their existing target. Update through HACS and restart Home Assistant.

Update available via HACS
