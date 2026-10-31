<!-- release: v2.12.1575 -->

## Tariff today timing

Rolling price schedules now display in Home Assistant's local timezone, including daylight saving and intervals crossing midnight. Previously, a schedule supplied in a different timezone could make the Tariff today prices appear an hour early.

This corrects the displayed schedule times and dates. Dynamic Peak, Shoulder and Off-peak labels still compare forecast prices rather than represent your network tariff's named periods. Update through HACS and restart Home Assistant.
