<!-- release: v2.12.1855 -->

## Start works again for generic chargers controlled by a simple on/off switch

On a generic EV charger set up with only an on/off switch (no connector status sensor), the EV panel still showed **Disconnected** and kept **Start** greyed out whenever the switch was off. The earlier fix for this was being undone when the dashboard combined its two readings of the same charger.

The panel now shows **Charger off** and keeps **Start** available, and pressing it turns the charger switch on. Chargers that have a connector status sensor are unchanged. Update through HACS, restart Home Assistant and refresh the dashboard page.
