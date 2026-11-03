<!-- release: v2.12.1615 -->

## Storm Watch activation notifications

PowerSync now creates a Home Assistant notification and sends a push to registered PowerSync mobile devices when Tesla reports Storm Watch active. The alert explains that storm protection may charge your Powerwall from the grid or pause normal discharge.

Repeated readings and integration reloads do not repeat the alert. This uses current Tesla cloud status and does not change Storm Watch or battery settings. Update through HACS and restart Home Assistant.
