<!-- release: v2.12.1510 -->

## Fixed

### Powerwall automatic charging

Fixed an optimiser charge failing to start when Automatic control selected Tesla tariff charging and Tesla's charge-from-grid setting was initially off. When PowerSync allows grid charging, it now enables and verifies that setting before starting, then restores the original setting afterward, including after a restart.

Existing site limits, battery charge ceilings, EV demand and control permissions still apply. Update through HACS and restart Home Assistant before retesting the next charging window.
