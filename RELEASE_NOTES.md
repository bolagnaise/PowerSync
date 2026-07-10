<!-- release: v2.12.816 -->

## What's Changed

**More reliable Sungrow low-SOC discharge recovery**
PowerSync no longer assumes that an unreadable reserve register means every Sungrow battery has a fixed 5% discharge floor. Low-SOC stale discharge-cap recovery now remains available unless an explicit reserve or a zero BMS discharge-current allowance confirms that the inverter is protecting the battery, preventing valid recovery from being masked on systems that normally discharge to a displayed 0%.

**Clearer Flow Power KWatch health diagnostics**
Flow Power price sensors now expose the latest KWatch attempt, consecutive failure count, last successful update, and coordinator update status. These attributes continue updating while fallback pricing is active, making it easier to distinguish a live KWatch outage from healthy AEMO fallback data and to confirm recovery back to the primary source.

Update available via HACS
