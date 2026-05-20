<!-- release: v2.12.444 -->

## What's Changed

**Fix EV solar-surplus charging when Sigenergy is curtailed**
PowerSync now treats a 99-100% home battery as full for solar-surplus EV allocation, so Sigenergy top-off charge power no longer reserves battery headroom away from the EV. This stops the controller from ramping Tesla/Tessie charging down when the battery is already full and zero-export curtailment is active.

**Keep active EV charging stable under zero-export curtailment**
When an EV is already charging from available solar while Sigenergy export is curtailed, PowerSync no longer subtracts the household buffer from that active EV load. This prevents the slow self-induced ramp-down seen when export was near zero but solar was still available behind curtailment.

Update available via HACS
