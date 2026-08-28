<!-- release: v2.12.1202 -->

## What's Changed

**Flow Power residential plans**
Adds explicit Battery Happy Hour, Flow Power 4Free, and Flow Home contracts, including the new four-hour export window and the 1 September 2026 plan boundary. Existing Flow Power accounts remain on their saved legacy rate and window until a plan is explicitly selected.

**Quota-aware optimisation and billing**
Models Happy Hour and 4Free allowances as measured daily or hourly energy quotas. PowerSync now preserves the billable post-quota rate, applies only the remaining premium as a bounded optimiser incentive, persists measured settlement, and reports base, marginal, remaining, and planned values without consuming quota from forecasts.

**Home Assistant and mobile settings contract**
Adds plan and region selection, rejects unsupported combinations such as treating all Queensland accounts as SEQ 4Free, exposes the resolved contract through the provider API and existing price sensors, and supports the matching PowerSync Mobile settings screen.

Update available via HACS
