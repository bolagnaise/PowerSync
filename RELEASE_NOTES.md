<!-- release: v2.12.1100 -->

## What's Changed

**Keep standalone Tesla BLE Smart Schedules on the selected vehicle**

Tesla BLE-only installations now ignore lingering Fleet API device-registry rows and retain each configured BLE bridge as its own Smart Schedule identity. This keeps mobile toggles and Home Assistant status readback aligned and prevents stale Fleet VIN profiles from attempting unrelated charger backends. Ambiguous profiles created by the previous identity reconciliation are removed fail-closed; review and re-enable each BLE vehicle's Smart Schedule after updating.

**Report FoxESS curtailment effect separately from price eligibility**

The DC Solar Curtailment sensor now reports `Active` only when the FoxESS controller has acknowledged curtailment and fresh, valid grid telemetry confirms export is no more than 250 W. An acknowledged command with material export, stale telemetry, or missing effect evidence is shown as `Pending`, while negative-price eligibility remains available as a separate attribute. The dashboard uses a distinct pending state instead of claiming export is blocked before the physical effect is confirmed.

Update available via HACS
