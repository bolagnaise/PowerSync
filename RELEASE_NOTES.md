<!-- release: v2.12.1588 -->

## Tesla charging recovery

Tesla charging can now recover when its charging-current reading was temporarily unavailable. Once fresh settings and stopped-vehicle data are verified, an old charging attempt no longer keeps blocking new EV or native Powerwall controls.

Recovery still requires the exact vehicle and control provider, two fresh confirmation samples, and no conflicting charging work. Update through HACS and restart Home Assistant.
