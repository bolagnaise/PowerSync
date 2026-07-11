<!-- release: v2.12.822 -->

## What's Changed

**Flow Power KWatch current price no longer freezes on non-Tesla display schedules**
For Sungrow and other non-Tesla systems, the display-only Flow Power tariff schedule now uses the live KWatch current interval as the active period's PEA input. This prevents the Current Import Price sensor and tariff schedule card from falling back to a flat default wholesale value when the KWatch 30-minute forecast starts later than the current half-hour.

The Tesla tariff-upload path keeps the existing stable 30-minute schedule behavior; this change is limited to the stored dashboard/sensor schedule for systems that do not upload a tariff to the battery cloud.

Update available via HACS
