<!-- release: v2.12.1552 -->
## Tesla Grid Charging on firmware 26.34.2

Fixes Local V1R charging being refused as “disabled or unknown” when Grid Charging is On on firmware 26.34.2, build 56d7ba06. The correction covers manual charging, native Max, scheduled charging, and permission restoration.

PowerSync requires fresh local firmware and configuration checks for this specific build. Other firmware builds and unreadable or invalid permission remain blocked. Existing SOC, site import, EV, Monitoring Mode, and ownership checks still apply.

Update through HACS, restart Home Assistant, and confirm Grid Charging is On in the Tesla app before retrying.
