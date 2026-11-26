<!-- release: v2.12.1860 -->

## Both Powerwall 2s now show next to a Powerwall 3

On sites with a Powerwall 3 and Powerwall 2 units, PowerSync missed the Powerwall 2s because the gateway names them in a different format. It showed a single "Follower Powerwall" with a wrong, near-empty charge level, and Battery Health listed one pack too few. Each Powerwall 2 now gets its own entry with its real charge level and capacity, and Battery Health lists every pack. Battery control is unchanged. Update through HACS and restart Home Assistant.

## Tesla Charge on Solar over the BLE bridge (new, opt-in)

If your car is paired with the PowerSync BLE bridge, PowerSync can now read and set Tesla's native Charge on Solar through the bridge, without Teslemetry or the Fleet API. New services check the Charge on Solar pairing between the car and the Powerwall and repair each step on request. This needs the optional `charge-on-solar` bridge firmware and hasn't been tried on a car yet, so treat it as early access.
