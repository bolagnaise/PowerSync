<!-- release: v2.12.762 -->

## What's Changed

**Flow Power billing-period TWAP**
Flow Power import pricing now estimates TWAP against the active billing period instead of relying only on a flat trailing average. The tracker blends billing-period-to-date actual prices with the trailing mean as a forward proxy, so early billing-period behaviour remains stable while late-period pricing converges toward the settlement TWAP that drives PEA import calculations.

**Billing day configuration and visibility**
Flow Power users can now set their billing day in the integration options, and the tracker exposes the billing-period TWAP components used internally. This helps align overnight charge decisions with the account's actual settlement period, especially during sustained wholesale price shifts.

Update available via HACS
