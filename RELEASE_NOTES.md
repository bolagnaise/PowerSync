<!-- release: v2.12.741 -->

## What's Changed

**GloBird ZeroHero Jul 2026 plan**
PowerSync now includes a separate `ZeroHero Jul 2026` preset for the current public GloBird terms: 10c/kWh total Super Export for the first 15 kWh from 6pm-9pm, the existing $1 no-import credit handling, and ZeroCharge free import from 12pm-3pm with a 50 kWh daily cap.

**Legacy and account-specific ZeroHero support**
Existing `previous 3-hour`, `legacy 2-hour`, and custom ZeroHero settings remain available and are not auto-migrated. Custom GloBird users can now enter account-specific ZeroCharge start/end times and import caps, including disabling the free-import window with a zero cap.

**ZeroCharge-aware optimisation and reporting**
The optimiser now models capped free-import value separately from normal import tariffs and evening Super Export behaviour, while still respecting grid-charge limits, import power caps, charge price caps, and SOC caps. Daily cost tracking and optimiser status now report ZeroCharge import usage, remaining cap, and credited value.

Update available via HACS
