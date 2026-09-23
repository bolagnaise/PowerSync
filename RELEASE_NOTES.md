<!-- release: v2.12.1332 -->

## Fixes

### Settings: battery method and Tesla API now have distinct paths

The battery method selector previously opened the selected system's connection
page even when the method had not changed. On Tesla installations this made
"Battery / control method" lead to the same options as "Tesla API connection".
Selecting the current battery method now returns to PowerSync settings without
rewriting configuration. Changing to another method still opens that method's
connection setup. The menu now labels the selection "Change battery / control
method" so its purpose is clear alongside the connection settings.

Update available via HACS
