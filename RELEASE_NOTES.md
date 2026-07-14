<!-- release: v2.12.840 -->

## What's Changed

**Sigenergy export limits now persist safely**

The Export Limit selected in PowerSync Controls is now saved as the persistent site cap and immediately applied to Smart Optimization. Restarting or returning to self-consumption no longer replaces a user-selected limit with the inverter's higher rated-power value, so the action plan cannot schedule export above the configured cap.

**Zero-export curtailment restores the configured cap**

Changing the Sigenergy cap during an active uneconomic-export window now updates the post-curtailment restore target without briefly lifting zero export or recapturing a stale live register. Failed Modbus writes roll back runtime state and do not advance saved configuration or the optimizer plan.

Update available via HACS
