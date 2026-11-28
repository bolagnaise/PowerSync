<!-- release: v2.12.1868 -->

## Charging cost for each car on a shared OCPP charger

When two or more cars share one OCPP charger, charging history recorded every session against the charger, so you couldn't see what each car cost to charge. PowerSync now works out which car each session belongs to from the RFID card that started it. If there's no card, it uses the car you selected for that charger, or the only vehicle set up on it. Sessions it can't match are listed as Unassigned.

Charging statistics now include cost, grid energy and average price per kWh for each car, plus totals for each charger. Give each driver their own card and start a charge with it. Then assign the card to its car in the PowerSync mobile app under EVs & Chargers → RFID cards (this needs the next mobile app update). PowerSync only reads the card that started a charge; it doesn't change which cards your charger accepts. Full card numbers are never stored.

A Tesla charging on an OCPP charger is also no longer recorded twice in charging history.

This applies to chargers connected through the HACS OCPP integration.

## EV charging no longer gets stuck behind old safety holds

Several safety checks could leave automated charging blocked with nothing to clear them, so Solar Surplus, Smart Schedule, Price-Level or Boost stopped starting the car until Home Assistant was restarted. They now clear themselves:

- Solar Surplus, Smart Schedule, Price-Level and Scheduled start again after the car was away, unplugged, charged by someone else, or after a Stop the car never confirmed.
- A Boost refused before any command was sent no longer blocks the next Boost.
- After a Home Assistant restart in the middle of a Tesla charge, Smart Schedule no longer gets stuck showing "Start not confirmed" and retrying. PowerSync releases the old session's command route once nothing about it is unresolved.
- A running Smart Schedule solar charge survives a short dip in surplus instead of stopping.
- An unknown home battery level is no longer read as 0%, so a data gap no longer pauses or stops a running charge.
- Solar Surplus, the Smart Schedule forecast and the optimiser now use the same household buffer and battery reserve, so the forecast matches what live charging does.
- "Min battery to start" now applies to Smart Schedule's grid and battery starts, as it already did in the plan.

## Fewer false "Charging control paused" alerts

While PowerSync wakes a Tesla and starts it over Bluetooth, which can take 40 seconds, the mobile app showed "Charging control paused" and Home Assistant logged that the car was held by recovery history, even though nothing was wrong. These now appear only when a command really is stuck.

Boost no longer shows "Could not change charging" while the car is still waking. If the start takes longer than 20 seconds, the app says Boost was accepted, and charging begins shortly after.

Update through HACS and restart Home Assistant.
