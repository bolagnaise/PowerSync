<!-- release: v2.12.1529 -->
## Powerwall grid-charging alerts

Local V1R and Spread Import charging requests now notify you when they are refused because Allow Grid Charging is off or unavailable, or the gateway's grid-charging permission is off or unknown. Home Assistant shows the reason, and registered PowerSync mobile devices receive a push.

Repeated refusals share one alert, which clears after a successful local charge. Leaving Allow Grid Charging off without a charging request stays quiet. Existing permission, temporary-enable and restoration checks remain in place.

Update through HACS and restart Home Assistant. This release also includes the native charging and discharge alerts added in the previous releases.
