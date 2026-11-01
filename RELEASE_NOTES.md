<!-- release: v2.12.1587 -->

## More reliable Tesla charging

Tesla charging now uses one controller per vehicle to coordinate schedules, Boost and manual commands. BLE remains the primary route when configured, with Fleet or Teslemetry fallback when allowed. Wake-up handling gives sleeping cars time to respond, and transport changes preserve each charging session's original deadline.

Recovery now distinguishes old commands from new ones, preventing an earlier recovery from blocking later charging. Expired Boost and manual sessions can also finish cleanly when fresh vehicle data confirms charging has already stopped or the cable is disconnected. Supported stock BLE bridges use software recovery without a firmware update.

## Powerwall controls alongside EV charging

Native Powerwall charging and export can run alongside managed EV charging when the site's required limits and control settings are verified. The site continues to manage its output with home and EV loads taking priority. Overlapping export-setting checks no longer cause a false control refusal while an existing confirmation is still fresh.

Update through HACS and restart Home Assistant. Selected two-car and transport paths have been tested locally; the complete two-car arrival-order matrix and every Teslemetry transport variant have not been verified on hardware.
