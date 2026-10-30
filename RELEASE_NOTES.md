<!-- release: v2.12.1563 -->
## Clearer battery control feedback

The dashboard now shows **Starting charge…**, **Starting discharge…** and **Restoring…** while battery controls are in progress, instead of displaying an active countdown during the transition. Local Powerwall requests update the dashboard before waiting for the gateway to acknowledge them.

## Restore Normal status

A manual Restore Normal no longer reports a failed action solely because Native Max settings have been restored while physical settling is still being checked. PowerSync keeps control ownership until verification finishes; actual restoration failures still report an error.

Update through HACS and restart Home Assistant. Refresh the dashboard after restarting to load the updated controls.
