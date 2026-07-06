<!-- release: v2.12.770 -->

## What's Changed

**ZeroHero export planning no longer forces low-value paid top-ups**
PowerSync now keeps GloBird ZeroHero export windows separate from priority export windows, so a capped Super Export period can still be used when it is genuinely useful without forcing the battery to top up from paid grid power purely to preserve a low-value export plan. This fixes plans that could import at shoulder/peak rates before exporting later at a lower effective return.

**Export priority remains available for explicit high-value windows**
Flow Power Happy Hour, export boost, and joined saving-session windows still receive priority export handling where PowerSync may deliberately protect export energy across the plan.

Update available via HACS
