<!-- release: v2.12.1645 -->

## Tesla BLE Stop recovery

Fixes a recovery retry that could remain blocked after a later unconfirmed Stop moved an earlier quarantine record into history. Recovery can now include that preserved record for the same original bridge and vehicle, while still requiring fresh proof from every affected bridge.

Update through HACS and restart Home Assistant. No companion firmware change is needed; keep EV automations and other writers paused when retrying the same complete inventory in the recovery guide.
