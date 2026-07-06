<!-- release: v2.12.775 -->

## GloBird ZeroHero Super Export fix

PowerSync now keeps GloBird ZeroHero Super Export optimisation active even if the separate no-import daily credit has already been lost for the day.

Previously, once evening grid import exceeded the no-import credit threshold, PowerSync also disabled the remaining capped Super Export bonus for that day. That could leave the optimiser in self-consumption during the 6pm-9pm Super Export window even though the site was still below the daily Super Export kWh cap.

With this release, the two credits are handled separately:

- exceeding the no-import threshold still marks the daily no-import credit as lost;
- any remaining capped Super Export kWh remains available to the optimiser during the configured Super Export window;
- priority-export handling continues until the Super Export cap is exhausted.

This is covered by focused ZeroHero settlement and optimiser regression tests.
