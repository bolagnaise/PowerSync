<!-- release: v2.12.1164 -->

## Three fixes: the Month cost average, a Charge By Time top-up bought too early, and a curtailment status that claimed more than it had done

All three were reported as retests or new tickets against v2.12.1160–v2.12.1163.

### Avg Cost per kWh (Month) still read Unknown after v2.12.1162

**Update if your Month row is still blank after updating to v2.12.1162 or v2.12.1163.**

v2.12.1162 added a one-time repair for a month whose *priced* energy was recorded as materially short of its *measured* energy — the state that withholds the month average until the 1st of the next month. The repair was correct in what it did and unreachable for the installs it was written for.

It only ran on a stored payload carrying no coverage-schema marker at all, on the reasoning that only v2.12.1132/1133 could write coverage counters without one. That was true of the original write, but not of the payload by the time the repair ran: every save stamps the *current* marker. So any build from v2.12.1134 onwards opened such a payload, skipped the older key-absence migration because the keys were present, and wrote it back carrying its own marker. Upgrading is exactly what does that. By the time v2.12.1162 arrived, the marker it was testing for was long gone, and the Month row stayed Unknown.

**What changed.** The repair is now pinned to the schema it ships in rather than to an older one, so it reaches a month whose coverage counters were started from zero mid-month no matter which builds have opened that payload since — including payloads v2.12.1162 and v2.12.1163 rewrote themselves. It still runs at most once per install, and a coverage hole that appears *after* the repair still fails closed and withholds the average rather than showing a wrong one.

The **day** rows are unchanged and still use the stricter rule: they reset at midnight and recover on their own within hours.

### Charge By Time went back to buying the top-up straight after the export window

**Update if you use Charge By Time and the grid top-up sits immediately after your evening export window, then holds SOC flat all night.**

v2.12.1140 and v2.12.1162 addressed this by holding the top-up back until the forecast solar still ahead of your deadline can no longer meaningfully contribute, and charging from that point on. On some sites that hold-until point was collapsing all the way back to "now", which turns the whole plan into buy-as-early-as-possible and makes both earlier fixes no-ops.

The cause is a scope mismatch in how forecast solar was credited for that one decision. PowerSync learns how much your solar forecast typically over-promises and keeps a robust **whole-day** allowance for it. That allowance was being subtracted, in full, from the solar surplus still ahead of the deadline — which on an afternoon deadline is only part of a day. Where your pre-deadline surplus is smaller than a typical daily forecast miss, the subtraction left nothing at all, at *every* point in the plan, so "the first moment solar can no longer help" evaluated to right now.

That put one solve in the position of holding two different beliefs about the same forecast: it still sized the top-up assuming solar would arrive, while placing it as though solar would contribute nothing.

**What changed.** The hold-until point is a question about whether forecast solar can still help at all, so it is now decided on the forecast without the learned allowance applied. The allowance keeps its job where reserving headroom pessimistically is the point — deciding how much SOC headroom to leave for solar to fill — and the plan is re-solved continuously, so a top-up placed later can still be shrunk or cancelled when solar over-delivers, while one bought early cannot be given back.

This is placement only. On the reproduction the energy imported before the deadline, the SOC reached at the deadline and the predicted cost are unchanged, and a genuinely cheaper overnight price still wins on price. A site with no forecast solar ahead of its deadline still charges from the earliest slot, so the Flow Power Happy Hour behaviour this was built for is untouched.

### DC Solar showed "CURTAILED - Export confirmed stopped" while the site was exporting

**Update if your curtailment card reads CURTAILED during negative prices while your meter shows export.**

For every battery brand except FoxESS, the DC Solar curtailment status was derived from the feed-in price alone: below 1 c/kWh it read `Active`, which the dashboard renders as "CURTAILED - Export confirmed stopped". It never consulted whether curtailment was switched on, whether a command had been sent, or whether the inverter had accepted one. A low price means a curtailment is *warranted*, not that one was performed.

That mattered most where no command was possible. A GoodWe system driven through the community GoodWe Home Assistant integration (and any GoodWe profile reached over Modbus TCP) has no direct export-limit surface, so the curtailment handler returned without sending anything — silently, at debug level. The card still read CURTAILED while the site exported ~6 kW at 100% SOC.

**What changed.**

- `Active` now requires curtailment to be enabled **and** an acknowledged control command behind it. Uneconomic export with no acknowledged command reports `Pending`, which the dashboard already renders as "PENDING - Export not confirmed".
- A GoodWe profile with no export-limit control surface now records that fact and says so once at warning level, instead of returning silently. The card reports PENDING with a description naming the reason, and the sensor exposes `control_state` and `export_uneconomic` attributes so the state can be read directly.

FoxESS is unaffected — it already reconciled its status against live grid telemetry, and that path is unchanged. Brands that do command curtailment (Sungrow, Sigenergy, AlphaESS, SolarEdge, GoodWe on a direct connection, and Tesla's grid export rule) continue to report `Active` once the command is acknowledged.

**Not changed by this release:** GoodWe entity-only profiles still cannot perform DC export limiting — this release makes that visible rather than adding the capability. The separate AC-coupled inverter shutdown toggle is also still only driven automatically for Sungrow batteries; on other brands it runs only through the `power_sync.curtail_inverter` service. Both are being tracked separately.
