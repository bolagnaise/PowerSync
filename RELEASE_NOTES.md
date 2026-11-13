<!-- release: v2.12.1698 -->

## Zaptec charging control and battery protection

Price-Level Charging now consistently respects hands-off charging and waits for fresh, valid Zaptec readings before taking over an externally started session. Disabling Price-Level no longer lets an external-session abort setting stop unrelated charging.

Pending PowerSync starts and stops retain battery protection until charger readings confirm the outcome. Invalid readings cannot confirm a stop, and completed charging releases its battery protection without repeatedly restarting the car. Temporary loss of Tesla vehicle data no longer drops a pending Zaptec command; ambiguous vehicle data blocks new starts while still allowing a price rejection shared by all candidates to request one guarded stop.

## Zaptec resumes and retries

Zaptec can reuse an unchanged current limit after a fresh installation readback confirms it, avoiding an unnecessary wait before resuming. Different limits continue to respect Zaptec's update interval. Failed commands use a bounded retry cooldown while retaining unresolved command responsibilities.

Update through HACS and restart Home Assistant.
