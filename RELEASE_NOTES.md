<!-- release: v2.12.1783 -->

## Tesla BLE Stop recovery

An earlier charging command that PowerSync can prove was never sent no longer blocks a retry of BLE Stop recovery. Recovery still checks the affected vehicle and requires fresh confirmation that it has stopped before clearing the hold.

Update through HACS and restart Home Assistant.
