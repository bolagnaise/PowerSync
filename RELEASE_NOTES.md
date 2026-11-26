<!-- release: v2.12.1861 -->
## Every Powerwall now gets its own dashboard card

If PowerSync found an extra Powerwall or expansion pack after your first setup, its charge, health and capacity sensors existed but the dashboard's Powerwalls section didn't show them. This happened, for example, with the second Powerwall 2 on a Powerwall 3 site after v2.12.1860. The section now shows a card for every pack. Update through HACS and restart Home Assistant. If you use the generated PowerSync dashboard, reload the dashboard page.

## EV charging rides out short data gaps

A running EV charge no longer stops because one reading briefly goes missing. This covers the price, the car's charge level, the plug state and the car's location. PowerSync allows 15 minutes for a missing price or car charge level to come back. An unknown home battery level is no longer treated as 0%. Missing or stale data still prevents PowerSync from starting a new charge.
