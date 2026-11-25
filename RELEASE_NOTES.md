<!-- release: v2.12.1828 -->

## Solar EV charging no longer over-reports surplus after a car speeds up

On Tesla Powerwall sites where the car's charging power comes from Tesla BLE or the Fleet API rather than a Wall Connector, Tesla's cloud site reading can arrive about a minute late. If the car had sped up in the meantime, PowerSync counted the extra charging power twice, so **Surplus** and **Allocated** could briefly show more power than your solar was producing. PowerSync now detects a site reading that is too old to include the car's current draw and uses the car's power from that moment instead. Readings that already include the car are unchanged.

Update through HACS and restart Home Assistant.
