# AGL seasonal tariff and daylight-saving investigation

Investigated on 3 November 2026 against Home Assistant source `337358f434`
(manifest 2.12.1608, pending publication at investigation start) and Cloud source
`04235cf85c11a062d8a2fb8eefd970cdd1066c12`.

## Confirmed Home Assistant defect

The TOU Schedule sensor built its chart and `current_season`, `buy_rates`,
`sell_rates` and `tou_schedule` attributes from the season flattened when the
tariff was loaded. Rebuilding at midnight reused that same snapshot. A static
tariff could therefore show the previous season after 1 November until another
tariff conversion or reload, even though current-price lookup and the optimizer
already selected each timestamp's season correctly.

The regression loads an October tariff with a 15:00 peak and a November tariff
with a 16:00 peak and distinct import/export prices. Advancing into November
without syncing reproduces a chart one hour early on the old code. Both normal
midnight rollover and updates a week apart fail before the repair. The repaired
sensor selects the current season through the shared tariff-calendar helper and
invalidates its cache on local date or UTC-offset changes. Saved tariff times,
rates, current-price evaluation and optimizer control are unchanged.

Regression coverage also checks authored local tariff hours before and after
Sydney's April and October DST transitions, plus Brisbane without DST. Existing
rolling/NEM timestamp display tests remain part of the adjacent checks.

## Tariff season versus DST

Sydney began daylight saving on **4 October 2026**, not 1 November. A change on
1 November can instead be a tariff season boundary. See the [RBA daylight-saving
calendar](https://www.rba.gov.au/schedules-events/daylight-saving.html).

AGL's [NSW standing-offer prices effective 1 July 2026](https://www.agl.com.au/content/dam/digital/agl/documents/terms-and-conditions/energy/rates-and-contracts/standard-retail-contracts/nsw/agl-electricity-standing-offer-prices-2026-nsw.pdf),
page 1, describe residential Ausgrid TOU as 15:00–21:00 every day in November–March
and June–August, with all-day off-peak in April–May and September–October.
Retail offers and demand tariffs can differ.

The integration's opt-in “Ausgrid seasonal TOU (configured plan)” instead
explicitly advertises summer weekday 14:00–20:00 and April–October weekday
17:00–21:00 in the setup descriptions. Those authored hours are not converted
from AEST by the tariff matcher. This preset must not be assumed to represent
the current residential standing offer, or silently shifted for all accounts.
The reporter's exact offer and saved tariff remain necessary to attribute their
particular mismatch. The screenshot alone does not establish which path they use.

## Cloud verification

Cloud has no equivalent AGL preset importer: this path reads the Tesla site's
raw tariff and IANA timezone. Calendar-sensitive current prices, optimizer and
billing evaluate the raw calendar for each instant; cached pricing retains that
calendar for season rollover. The static chart is built from locally evaluated
half-hour rates.

The Worker calendar, cost-pricing coverage, tariff-authority and TypeScript
checks passed. A separate temporary AGL-shaped fixture checked 31 October to
1 November, Sydney local 14:59/15:00/20:59/21:00 with distinct peak/off-peak rates,
the normalized dashboard slots, cached-calendar billing and both 2026 DST
transitions. All passed; the equivalent Cloud defect was not reproduced.
Cloud's 48 wall-clock display slots remain a representation limitation on
23/25-hour transition days, separate from a persistent November shift.

No customer settings or hardware were changed. Publication of the HA repair
does not establish installation or the reporter's live recovery.
