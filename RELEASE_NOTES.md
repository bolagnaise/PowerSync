<!-- release: v2.12.1710 -->

## Complete support diagnostics

New support sessions now select all six diagnostic feeds by default. You can review or deselect feeds before pairing, and access still requires your approval. Past history and HA Recorder sharing remain separate choices.

## Tesla charging preparation details

Support recordings can now show why Tesla Fleet command preparation was refused, including the source-binding check and vehicle-data timestamps before and after the existing refresh. This improves diagnosis of charging failures; it does not change charging safety checks or confirm that charging has started.

## Stable optimizer plan updates

The dashboard keeps the last completed plan while reserve adjustments and other planning stages finish. This prevents a temporary first-pass export from appearing as the final schedule during recalculation.

## Powerwall control-method changes

After a confirmed restoration, changing the Powerwall control method now clears the previous optimizer session before replanning. If restoration cannot be confirmed, PowerSync keeps the pending session and refuses the change.

Update through HACS and restart Home Assistant. The new details are recorded during an approved session with control observations or diagnostic events enabled, or while local recording is enabled. No extra charging attempts are needed.
