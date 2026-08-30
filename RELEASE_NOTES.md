<!-- release: v2.12.1210 -->

## What's Changed

**Correct Home Load in Energy Flow while a generic charger is active**
When PowerSync already reports Home Load with observed EV charging excluded, the dashboard now preserves that value instead of subtracting the charger draw a second time. This prevents Home Load incorrectly appearing as 0 W during a generic Wall Connector charging session.

**Keep legacy generic-charger dashboards compatible**
Dashboards whose Home Load source does not declare an EV-load basis retain the existing generic-charger adjustment, so gross site-load sources still display the EV draw only once.

Update available via HACS
