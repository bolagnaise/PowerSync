<!-- release: v2.12.1215 -->

## What's Changed

**GoodWe curtailment retry remains bounded when export does not converge**
For direct GoodWe profiles, PowerSync now makes at most one early retry after a
successful zero-export register write still has fresh evidence of material grid
export. It keeps curtailment `Pending` until export is physically confirmed
below the safety threshold, then falls back to the normal periodic enforcement
cadence rather than repeatedly replaying the command.

**Flow Power plan rates are no longer inferred from the NEM-region selector**
The Flow Power NEM-region selector now names only the wholesale-price region.
Current plan rates continue to come from the explicitly selected plan, plan
region, effective date, quota state, and account-specific settings. The shared
cost-price path now also understands Flow's current dollar-denominated marginal
provider contract.

**AI explanation timeout feedback survives a Home Assistant card reload**
When an AI provider times out, the Home Assistant card now continues to show
the retry guidance after reload. If an earlier explanation is still available,
it stays visible with the failed-refresh notice. Monitoring Mode remains
descriptive-only and does not suppress explanations.

Update available via HACS
