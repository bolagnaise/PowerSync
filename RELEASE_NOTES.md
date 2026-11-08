<!-- release: v2.12.1653 -->

## Clearer Tesla charging holds and complete request history

Finite Tesla charging controls now identify retained unknown native commands, invalid recovery history and unavailable entities instead of incorrectly reporting every refusal as a changed producer binding. Future Fleet, Teslemetry and Tessie current/charge-limit attempts save their requested HA value and unit before dispatch.

The new **Inspect Tesla EV recovery status** action shows one exact vehicle's retained attempts without refreshing or waking the car, issuing controls, or changing recovery history. Specify the PowerSync entry when multiple entries are loaded.

These changes do not unlock older unknown commands or reconstruct their missing values. Existing recovery holds and location/plug checks remain in effect. Update through HACS and restart Home Assistant for the corrected diagnostics and future request recording.
