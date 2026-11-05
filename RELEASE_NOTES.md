<!-- release: v2.12.1632 -->

## Powerwall restoration retries

Native Max cleanup now waits at least 30 seconds between failed background attempts instead of retrying twice in quick succession and repeating every second. This reduces repeated tariff requests and log flooding when tariff readback is unavailable or restoration cannot yet be verified.

Pending recovery records remain protected until settings, tariff and physical settling are confirmed. This update does not bypass an ownership mismatch or guarantee that an upstream outage has cleared. Update through HACS and restart Home Assistant, leaving the saved recovery record unchanged.
