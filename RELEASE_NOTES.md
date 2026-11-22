<!-- release: v2.12.1796 -->

## Choose the grid price for each car

In the Home Assistant EV panel, expand **Modes** and find your car under **Smart Schedule**. Set **Maximum grid price** under **Default strategy**, or give a weekday its own limit. A grid period meets the price rule at or below the amount you enter. You can use zero or a negative price. **Meet Deadline** can still use a more expensive period to reach the departure target.

A new price applies at the next charging decision. When settings conflict with another edit or home-battery rule, the panel shows what to review beside the car’s Save button.

## Tune solar charging

Solar Surplus now shows how spare solar is measured and accepts fractional feed-in prices. Price Level and Scheduled Charging keep their other battery and grid settings when you edit a price.

Update through HACS and restart Home Assistant.
