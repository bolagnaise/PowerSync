<!-- release: v2.12.1562 -->

## Automatic AI explanations

You can now choose how often automatic AI explanations may refresh. In **Settings → Devices & services → PowerSync → Configure → Smart Optimization → AI Plan Explanation**, enable **Automatically refresh explanations** and set **Minimum automatic explanation interval (minutes)** to **30** to reduce requests from five-minute price updates.

The interval accepts 5–1440 minutes and keeps the existing five-minute default. PowerSync explains the newest changed plan after waiting; manual refresh remains immediate. Restarting Home Assistant resets the interval timer.

Update through HACS and restart Home Assistant. This setting affects explanations; battery optimization continues on its normal schedule.
