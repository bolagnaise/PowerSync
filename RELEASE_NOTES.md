<!-- release: v2.12.1365 -->

## What's Changed

**Keep solar refill available before a later premium export window**
When stored battery energy has a small positive acquisition cost, a lower
feed-in-price slot can still be profitable while forecast solar surplus is
available. Smart Optimization now keeps that solar-only refill path reachable
before a later premium export opportunity, so a Charge By Time deadline does
not reserve away the current export window unnecessarily.

Grid-import-to-export passthrough remains blocked, and explicit charge,
priority-export, manual-control, price-validity, and reserve safeguards remain
in force. This changes planning eligibility only; it does not change inverter
command units or claim live hardware behavior.

Update available via HACS
