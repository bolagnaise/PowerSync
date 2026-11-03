# AGL seasonal tariff and daylight-saving investigation

Investigated on 3 November 2026 against Home Assistant source `337358f434`
(manifest 2.12.1608, pending publication at investigation start) and Cloud source
`04235cf85c11a062d8a2fb8eefd970cdd1066c12`.

## Confirmed automatic AGL preset defect

The reporter's 3 November attachments show the selected “AGL / Ausgrid Seasonal
TOU” preset with 14:00–20:00 peak import. The account's AGL terms instead require
15:00–21:00 every day in November–March and June–August, with all-day off-peak
in April–May and September–October. The original approved feature request in
ticket #355 specified that same calendar; the implementation shipped a different
weekday/demand-style calendar. This is a current-code preset defect, not an
extra DST conversion applied by the tariff matcher.

The correction authors the approved local-time calendar, keeps the configured
summer and April–October off-peak prices, and applies winter peak prices only
in June–August. The existing evening export overlay remains 17:00–21:00.
Existing installs repair the saved tariff before startup price calculation only
when the automatic preset is explicitly selected and its old generated calendar,
rates and export overlay are unchanged. Missing/manual selection and edited
tariffs are preserved. Saved tariffs take precedence over the original setup
snapshot so a later setup stage cannot undo the repair or a manual edit.

The relevant Discord evidence is the [original account terms](https://discord.com/channels/1443943629130567874/1534147378343313428/1537009280123801662)
and the [3 November screenshots](https://discord.com/channels/1443943629130567874/1546066744467263518/1566985416257568788).
The screenshots were downloaded with size/signature receipts and visually
inspected. A prior support response recommended manual tariff entry; this
investigation additionally reconciled the approved feature scope with its
implementation.

The account screenshot also mentions a morning export window. Extending the
existing evening-only Battery Rewards overlay is a separate tariff feature;
this repair corrects import timing and preserves configured export rates.

## Confirmed seasonal chart defect

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

The old setup descriptions repeated the incorrect preset's weekday hours.
They are corrected together with the builder; differing account offers continue
to use manual tariff entry. No blanket one-hour timezone shift is applied.

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

The actual HA optimizer timestamp generator also passed Sydney's skipped and
repeated hours with constant elapsed interval spacing. Its actual TOU forecast
method passed the November season boundary and authored 15:00–21:00 local peak
windows across both DST changes.

No customer settings or hardware were changed. Publication of the HA repair
does not establish installation or the reporter's live recovery.
