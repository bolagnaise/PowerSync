<!-- release: v2.12.1173 -->

## What's Changed

**Daily Cost Tracking no longer presents partial month coverage as complete**
PowerSync now preserves the recorded priced-energy coverage when restoring a
month-to-date accumulator. A saved schema marker cannot prove that every
metered interval had a tariff price, so a genuinely partial import or export
total remains `Unknown` instead of producing a misleading Month average.

Older payloads that predate priced-coverage counters entirely still migrate as
before. Where PowerSync cannot prove full coverage, it now reports the
fail-closed state until fresh priced telemetry establishes a complete period.

Update available via HACS
