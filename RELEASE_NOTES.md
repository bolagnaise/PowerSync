<!-- release: v2.12.1162 -->

## Avg Cost per kWh (Month) stuck on Unknown, and a Charge By Time top-up placed too early

Two unrelated fixes. The first affects anyone whose monthly cost average went blank and stayed blank; the second affects anyone using Charge By Time with a target set for the following day.

### Avg Cost per kWh (Month) read Unknown and updating did not clear it

**Update if the Month row on your Daily Cost Tracking card reads Unknown while the three day rows above it keep reporting normally.**

That specific pattern — three live day-scoped rows, one blank month row — meant the month's *priced* energy was recorded as materially short of the month's *measured* energy, so the month average was withheld rather than shown wrong. It clears only at a month rollover, which is why updating the integration appeared to do nothing at all.

Two different pieces of stored month state produce it, and neither had any recovery path short of waiting for the 1st.

**Coverage counters that started from zero mid-month.** v2.12.1132 and v2.12.1133 introduced the counters that track how much of your metered energy carried a price. Those two builds restored the month's measured energy in full but began its coverage counters at zero part-way through the month, so everything imported earlier in that month counted as measured-but-unpriced. v2.12.1134 added a repair for this, keyed on the counters being *absent* — but v2.12.1132/1133 write those very keys, so the repair could never fire on the installs that most needed it. The shortfall was never a real pricing hole; it was exactly the part of the month that ran before the upgrade.

**A Home Load month flag that was set but never cleared.** Before v2.12.1153, a sample that integrated nothing at all — the first reading after a restart, or one arriving past the six-minute staleness guard — could flag the month's Home Load accounting as incomplete. v2.12.1153 stopped that flag being raised wrongly, but left every flag already written to disk in place, and the flag is cleared only at a month rollover.

**What changed.** Both are now repaired once, on restore:

- A stored month whose coverage counters are present but were started from zero has its coverage adopted. Only v2.12.1132/1133 can produce that combination, so this cannot alter any month recorded by a later build.
- A Home Load month flag written before this release is cleared once. If your Home Load genuinely has a hole, the very next reading that is missing load raises it again, so nothing is hidden.

The **day** rows deliberately keep the stricter rule and still fail closed on a short count, because they reset at midnight and recover on their own within hours.

**A note on a related, non-fault case.** If your plan has a free-import window — CovaU SolarMax free-import quota, for example — a day spent importing inside that window has a true cost of $0.00, and Estimated Import Cost Today, Export Earnings Today and Avg Cost per kWh (Today) will all correctly read zero. That is not a stalled sensor. This release also adds a guard so a legitimately free window can never be mistaken for missing price data and blanked.

### Charge By Time bought its top-up a block too early

**Update if you use Charge By Time and set a target in the evening for the following afternoon.**

v2.12.1140 fixed the large version of this: a target armed at night for the next afternoon was committing the entire grid top-up immediately, ahead of a whole forecast solar day. It works by holding the top-up back until the forecast solar still ahead of your deadline can no longer meaningfully contribute, and charging from that point on.

It calculated that hold-until point correctly, but the solver could not act on it. Placement is decided by a tiny tie-breaking nudge — far too small to override a real price difference, and only used to settle a genuine tie. The nudge before the hold-until point and the nudge after it were measured on the same scale with nothing separating them, so they overlapped. For an evening-armed next-day target the cheapest slot in the whole plan ended up being the one immediately *before* the hold-until point, and the solver filled backwards from there.

The result: the grid charge block **ended** where it should have **begun**, running a full block-length early — back into the stretch where forecast solar could still have filled that headroom for free, and where a later re-solve could still have shrunk or cancelled it.

**What changed.** The two nudges no longer overlap, so every slot after the hold-until point is preferred over every slot before it. The ordering within each stretch is unchanged.

This is placement only. On the reproduction the total energy imported before the deadline, the SOC reached at the deadline and the predicted cost are all identical to three decimals, and a night that is genuinely cheaper still wins on price as before. The Flow Power Happy Hour behaviour this bias was originally built for is untouched.

**Not changed by this release:** a control to spread import across the last hours before your export window remains a feature request, and a shortfall at a high SOC target with Charge By Time off is high-SOC battery charge taper, which is a separate item.
