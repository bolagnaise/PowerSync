<!-- release: v2.12.1583 -->

## Tesla Grid Charging in automations

Fixed a timeout that could make Grid Charging report a verification failure and stop an automation after Tesla accepted the request and repeatedly omitted the setting from valid replies. Slow final checks now preserve that accepted result; invalid or contrary replies still fail verification.

Update through HACS and restart Home Assistant. Acceptance remains separate from confirmed settings and actual charging; check fresh Battery/Grid Power after the next normal automation run.
