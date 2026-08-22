<!-- release: v2.12.1180 -->

## What's Changed

**Sungrow Month Average remains Unknown after a material daily coverage gap**
When a Sungrow daily energy register proves that a materially unpriced import
or export gap occurred, PowerSync now keeps the Month Average **Unknown** for
that month. The result is retained across Home Assistant restarts and the daily
rollover, so a small daily gap cannot be hidden by a large month-to-date total.
Normal register/sampling differences within the existing daily tolerance still
remain valid. This is accounting/status only and does not change inverter
control.

Update available via HACS
