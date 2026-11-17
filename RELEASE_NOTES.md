<!-- release: v2.12.1735 -->

## Tesla charging recovery with Tessie

PowerSync can now obtain a fresh vehicle reading from Tessie when its cached data cannot confirm charging recovery. This fixes a gap that could leave charging plans waiting for vehicle confirmation.

Update through HACS and restart Home Assistant. Existing command-history and vehicle-identity checks remain in place.
