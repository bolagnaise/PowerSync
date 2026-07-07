<!-- release: v2.12.786 -->

## What's Changed

**Automation time triggers use the Home Assistant timezone**
PowerSync app automations now fall back to Home Assistant's configured timezone when a site-specific timezone is not available. This fixes time-only automations firing 30 minutes early for South Australian/custom-tariff installs that previously fell back to Sydney time.

**Priority export windows work in the greedy optimizer fallback**
The greedy fallback solver now keeps explicit priority export windows active even when the normal acquisition-cost guard would otherwise cap export to house load. This keeps ZeroHero-style capped export bonus windows exporting correctly if the LP solver is unavailable or falls back.

**Battery restore failures retry instead of getting stuck**
The optimizer now keeps retry state when no-discharge release or self-consumption restore commands fail. A transient failed restore should no longer permanently clear the active flag and leave a battery stuck in a no-discharge or prior forced-control state.

**Sigenergy self-consumption restores ESS limits**
Sigenergy self-consumption restore now uses the full normal-restore path, so ESS charge/discharge limits are reset after force or no-discharge windows instead of only writing Remote EMS mode.

Update available via HACS
