<!-- release: v2.12.1154 -->
# v2.12.1154

## FoxESS DC solar curtailment no longer switches itself back off mid-window

**Who this affects:** FoxESS sites with DC solar curtailment enabled, on any backend (Modbus, entity bridge or Cloud).

### The bug

Curtailment only needs to block *material* live export — if solar is already being fully self-consumed or absorbed by the battery, there is nothing to curtail. That check ("import price is non-negative and grid export is 250 W or less") was correctly used to decide **not to start** curtailing, but it was also being used to decide to **stop**.

Those are not the same thing. Once curtailment is applied, grid export sitting near zero is the *result* of the command, not a sign the command is unnecessary. So on a site with a full battery:

1. Prices go negative, PowerSync curtails, export drops to ~0 W.
2. Five minutes later the curtailment check reads "export is only 20 W" and restores — **while the feed-in price is still uneconomic**.
3. Export resumes, and five minutes after that the check curtails again.

The loop never converged. Measured against a constant negative feed-in price, a 60-minute window produced 7 curtails and 6 spurious restores, leaving the site **uncurtailed for 30 of those 60 minutes** — paying to export for half of every negative window. The DC Solar Curtailment sensor flapped `Normal → Pending → Active → Normal` on a 5-minute period, which is what this looked like from the outside.

### The fix

The no-material-export check is now an **entry guard only**. An already-curtailed inverter holds the curtailment for the whole uneconomic window. Leaving that window is owned solely by the price branch, which releases once export earnings recover past 1.2 c/kWh — exactly once, as it always should have.

The intended behaviour is unchanged: when export earnings are uneconomic but the site is not materially exporting, PowerSync still issues no command at all.

### Re-apply timing

`curtail()` writes a FoxESS remote-control window with a 600-second timeout, and the curtailment check runs every 5 minutes. The re-apply interval was 480 s, which meant the first check with enough elapsed time was the one at **600 s** — level with the expiry it exists to stay ahead of, not ahead of it. Previously the oscillation masked this, because a fresh `curtail()` was being issued every 10 minutes anyway. With the self-restore removed this timer is the only thing holding the inverter curtailed, so it has been lowered to 240 s: the re-apply now lands at the 300-second check, with 300 seconds of margin before the hardware window expires.

### Scope

FoxESS only. No other brand's curtailment handler carries this pattern; Sigenergy, AlphaESS, GoodWe, SolarEdge, Sungrow and the AC-inverter handler are untouched.

### Regression coverage

- A successful curtailment must not be read back as a reason to restore: eight consecutive checks at a constant uneconomic price now issue exactly one `curtail` and zero `restore` commands, against a fake whose grid telemetry responds to the commands the way real hardware does.
- The entry guard still suppresses the initial command when export is naturally negligible.
- Price recovery still restores exactly once, and a failed restore still keeps curtailment ownership and its timer for a safe retry.
- The re-apply now fires at the 300-second check and not before.
