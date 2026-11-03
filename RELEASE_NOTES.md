<!-- release: v2.12.1610 -->

## Preserve the tariff chosen during AGL setup

A fresh AGL setup now uses the tariff selected during setup, even if an old tariff remains from a previous installation. This corrects a setup edge case in v2.12.1609 while keeping the guarded repair for existing automatic presets and preserving manually edited tariffs.

This release includes the corrected AGL/Ausgrid import calendar: peak 3–9 pm every day in November–March and June–August, with all-off-peak April–May and September–October. Seasonal charts refresh automatically, and configured prices and the evening export reward remain preserved.

Update through HACS and restart Home Assistant.
