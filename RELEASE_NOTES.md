<!-- release: v2.12.1576 -->

## Tesla control on older setups

Older Powerwall setups could reject manual and optimizer Tariff charging with a Native Max policy error, and refuse changes to the preferred battery control method. PowerSync now recognizes these saved Tesla sites correctly so eligible requests follow the selected control method.

Existing recovery and EV safety checks still apply. Updating does not clear an unresolved local settings incident. Update through HACS and restart Home Assistant.
