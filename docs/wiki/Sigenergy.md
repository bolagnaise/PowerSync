# Sigenergy

PowerSync supports Sigenergy systems through two separate paths:

- **Sigenergy Cloud API** uploads the current tariff so the Sigenergy app can show matching tariff graphs.
- **Modbus TCP / Remote EMS** reads live power data and lets PowerSync control charge, discharge, reserve, and curtailment.
- **Sigenergy EVAC / EVDC charger support** can be configured separately for EV charging, using the charger type, host, port, and slave ID exposed through PowerSync's EV charging settings.

## Sigenergy Cloud region

Select the Sigen Cloud region that matches your mySigen account when configuring Cloud credentials:

- Australia / New Zealand: `api-aus.sigencloud.com`
- Europe: `api-eu.sigencloud.com`
- United States: `api-us.sigencloud.com`
- Asia-Pacific: `api-apac.sigencloud.com`
- China: `api-cn.sigencloud.com`

Leave **Device ID** blank unless your account exposes a 13-digit `userDeviceId`. EU accounts commonly expose `user_id`, `stationId`, and `stationCode` instead; those are not Device ID values. Use `stationId` only in the Station ID step when the station list is unavailable.

## Smart Optimization requirements

When **Smart Optimization** is enabled, PowerSync's LP optimizer must own battery dispatch. Configure the Sigenergy system so native tariff optimisation does not compete with PowerSync:

1. Disable Sigenergy **AI Mode** or any native cloud tariff optimisation that automatically charges/discharges from the tariff.
2. Enable **Remote EMS** control for the Sigenergy system.
3. Keep Sigenergy Cloud credentials configured in PowerSync if you want the Sigenergy app tariff graph to match the tariff PowerSync is using.
4. Keep Modbus TCP configured so PowerSync can read live power data and issue Remote EMS commands.

In this mode, PowerSync continues uploading the tariff to Sigenergy Cloud for app visibility, but LP dispatch decisions are sent locally through Modbus/Remote EMS.

## Native optimisation mode

If you use Sigenergy's own AI/native optimisation instead of Smart Optimization, Sigenergy Cloud can use the uploaded tariff as its scheduling input. PowerSync will still sync the tariff from supported price providers, but Sigenergy owns the battery scheduling decisions.

## Troubleshooting

### Sigenergy app tariff graph is stale

- Confirm Sigenergy Cloud credentials and station ID are configured.
- Confirm auto sync is enabled in PowerSync.
- Run the `power_sync.sync_tou` service and check Home Assistant logs for `Sigenergy tariff synced successfully`.
- If Smart Optimization is enabled, confirm the system is in Remote EMS mode rather than Sigenergy AI mode.

### Battery behaves differently from the LP plan

- Confirm Sigenergy AI/native optimisation is disabled.
- Confirm Remote EMS is enabled.
- Check logs for PowerSync force charge/discharge or optimizer actions.
- Avoid manually changing Sigenergy app operating modes while Smart Optimization is active.
