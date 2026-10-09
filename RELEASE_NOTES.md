<!-- release: v2.12.1366 -->

## What's Changed

- Fix Tesla optimizer force-discharge ownership so a confirmed optimizer command keeps its conditional 20-minute commitment across rolling-plan refreshes, while reserve, price, export-eligibility, and unconfirmed-readback safety gates can still release it immediately.
- Preserve the Tesla force-discharge commitment metadata across service state, persistence, and refresh paths; degraded or externally owned cleanup states cannot inherit an optimizer hold.
- Add regression coverage for a sliding self-consumption solve after a confirmed Tesla export command and for accepted-but-unconfirmed cleanup state.

Update available via HACS
