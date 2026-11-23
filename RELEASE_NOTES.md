<!-- release: v2.12.1799 -->

## Reconnecting Home Assistant

The HA optimiser access screen now explains how to reconnect a server you previously disconnected and what to do if you closed the pairing dialog or the code expired. Keep the original dialog open while approving your code, then return there to finish pairing.

## Cost Neutral charging during free electricity

Cost Neutral can now plan grid charging for the home battery while a solar-only EV uses surplus solar. It also keeps valid plans when two EVs share solar and only one may charge from the grid.

## Switch-only EV charging in Price Level

With **Prioritise switch-only charging in Price Level** enabled, PowerSync now waits until the full charger load fits the site's import limit and any active EV charging plan before turning the switch on. It checks again after a configured wake delay, so a rise in household demand during wake can hold the start.

Cloud-backed chargers can take time to show power. PowerSync accepts fresh charging status or measured power after the switch command. The default confirmation window is 150 seconds and covers a 90-second reporting delay.

Update through HACS and restart Home Assistant.
