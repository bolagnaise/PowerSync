<!-- release: v2.12.1200 -->

## Fixed

- Keep an active Smart Schedule EV charging during a live free-price period
  when the optimizer still carries a conflicting zero-power slot. The override
  is short-lived and exact-VIN scoped; charger, phase, battery-reservation, and
  site-import limits remain enforced.
- Allow **Manual Start** from the Home Assistant EV dashboard while Smart
  Schedule or another PowerSync automation owns the loadpoint. The backend's
  existing manual-takeover and charger-safety checks remain authoritative.
- Recover from stale 15/16 A Tesla capability ceilings only after a uniquely
  associated Wall Connector and fresh VIN-scoped current and power telemetry
  physically prove the higher charging rate. Configured and site limits still
  bound the recovered rate, while ambiguous or uncorroborated sources remain
  fail-closed.
