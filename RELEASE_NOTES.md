## What's New

### Powerwall Off-Grid & Reconnect Control
You can now take your Powerwall off-grid and reconnect directly from the PowerSync app — no Tesla app needed. This works on both Powerwall 2 (local REST) and Powerwall 3 (signed cloud commands via your paired RSA key). Tap the Go Off-Grid or Reconnect buttons in Controls > Local Grid Control, or use the `powerwall_go_off_grid` and `powerwall_reconnect_grid` actions in automations to trigger islanding based on price, time, or other conditions.

### Off-Grid Curtailment
When enabled in Battery Setup > Local Control, PowerSync can automatically take your Powerwall off-grid during negative price periods to avoid paying to export. It reconnects when prices recover. Configurable safety gates include a minimum SOC floor and a daily duration cap to prevent excessive cycling.

### Smarter Grid Status Notifications
Grid status change notifications (outage detected / power restored) no longer fire on every HA restart. They only trigger on actual grid transitions, and correctly distinguish between on-grid and off-grid states.

---

### App Improvements

- **Quick Actions** — Duration selector simplified from 12 buttons to 6 clean pills (15m, 30m, 1h, 2h, 3h, 4h)
- **Automations** — Action type selector redesigned from scattered chips to a clean list with checkmarks
- **EV Charging Flow** — Animation now flows from home to car (was reversed), with separate flow lines for dual-EV setups
- **VPP Programs** — Card hidden when no programs are available instead of showing an empty section
- **Settings** — Home Assistant setup and Appearance moved to dedicated screens, keeping the main settings page clean
- **Pairing** — Customer password auto-derived from WiFi password (last 5 chars) — no more manual entry

Update available via HACS
