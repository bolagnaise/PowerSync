# Flow Power Happy Hour Plus

Select **Happy Hour Plus (40c)** in the Flow Power plan selector during setup or
in the integration's Flow Power options when your written contract matches this
offer:

| Local time | Export credit |
| --- | --- |
| 17:30–21:30, first 15 kWh each day | 40c/kWh |
| 17:30–21:30, additional exports | 10c/kWh |
| Outside the window | 0c/kWh |

This preset matches the supplied Happy Hour Plus offer. It is a separate choice
from Battery Happy Hour; existing selections retain their rates. If your terms
differ, use Account-specific contract and its custom export tiers instead.

The 15 kWh allowance covers measured grid exports in the window, including solar
and battery exports. It resets on the next tariff day in Home Assistant's tariff
timezone. Planned exports do not consume the measured allowance.

The current price sensor shows 40c/kWh while measured allowance remains, then
10c/kWh when it is exhausted. When today's quota cannot be established from
telemetry, PowerSync uses the 10c base rate until coverage is established. Future
days have separate allowances. Static tariffs sent to battery systems use the
10c base rate; the optimizer accounts for the limited premium separately.

Selecting this plan does not switch your retailer contract. Only select it after
confirming the plan and effective date with Flow Power.
