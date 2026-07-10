<!-- release: v2.12.820 -->

Fixes:
- Fixed optimizer export execution at tariff boundaries when the cached first export-price slot is not the selected action's slot. This prevents Flow Power Happy Hour / priority export actions from being downgraded to self-consumption just because the previous slot had a zero export price.
- Includes the pending optimizer bridge-floor fixes so export reserve floors stop at the next real export run and import-bonus recharge windows are treated as cheap recharge opportunities.
