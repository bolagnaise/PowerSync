<!-- release: v2.12.1482 -->
## Paired Powerwall Max charging

Manual and automation Max charge requests can now use Tesla-managed charging above a gateway's timed-command ceiling when the site's permissions and import limits allow it. Numeric kW targets and Spread Import retain precise local control; no extra enable switch is needed.

PowerSync restores the captured tariff, mode and reserve when Max ends. Native charging can take about a minute to settle after settings are restored, so PowerSync keeps cleanup pending until measured power returns to its previous charging level. Restart recovery restores the session instead of replaying Max.

## Current control inside Tesla charge windows

An optional per-vehicle **Adjust rate in Tesla windows** setting lets PowerSync adjust positive charging current while the car is already charging at home in a supported recurring Tesla window. It defaults off; Tesla keeps control of starting, stopping and the charge limit. The control is available in the matching mobile update.

Native-window rate control and PowerSync-managed EV charging share site headroom with local Powerwall charging. Current restoration and uncertain commands retain their capacity reservation until fresh readings confirm settlement. A supported Tesla integration, fresh measurements and a known site import limit are required; start-only and finish-by-only schedules remain forecast-only.
