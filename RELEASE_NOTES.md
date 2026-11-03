<!-- release: v2.12.1618 -->

## Faster EV home-battery preservation

With **Preserve home battery** enabled, Price-Level, Scheduled charging and Smart Schedule now apply preservation changes without waiting for the next five-minute optimiser cycle. This removes the repeated control-cycle wait when Price-Level charging stops and restarts as prices cross the threshold.

Monitoring mode, manual battery controls and command failures can still prevent or delay a hold. Update through HACS and restart Home Assistant.
