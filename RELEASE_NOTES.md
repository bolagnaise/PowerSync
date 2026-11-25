<!-- release: v2.12.1835 -->

## EV headroom recovery works after PowerSync is removed and re-added

If PowerSync was removed and added again while a Tesla charging reservation was still held, that reservation stayed attached to the old PowerSync entry. **Recover EV charging headroom** refused it with `reservation_scope_mismatch`, and nothing could ever release it.

Recovery can now take over a reservation from a PowerSync entry that no longer exists, as long as it belongs to the same Powerwall site. The reservation is only released after fresh readings show the car has stopped. **Inspect Tesla EV recovery status** now shows whether the saved entry and site match, and reports `reservation_entry_removed` when the reservation can be recovered this way.

Update through HACS and restart Home Assistant.
