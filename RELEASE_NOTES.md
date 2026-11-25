<!-- release: v2.12.1837 -->

## Start a smart-plug EV charger from the dashboard

If your generic EV charger is controlled only by a smart switch, with no connector status sensor, the EV panel used to show **Disconnected** whenever the switch was off and disable **Start**. That meant you couldn't turn the charger on from the dashboard. PowerSync can't tell whether the car is plugged in without a status sensor, so the panel now shows **Charger off** in that case and keeps **Start** available. Smart Schedule already treated these chargers as ready, so scheduled charging is unchanged.

The **Shared charger vehicles** section now says that switching between vehicles needs a connector status sensor, rather than asking you to unplug a car it cannot detect.

Update through HACS, restart Home Assistant, then refresh the dashboard.
