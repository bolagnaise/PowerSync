<!-- release: v2.12.1808 -->

## FoxESS export limits after curtailment

PowerSync now remembers the previous FoxESS export limit when it temporarily lowers that limit. When export becomes worthwhile again, it can restore the saved value even if Force Discharge started in the meantime. An existing 0 W limit that PowerSync did not apply is left alone.

Update through HACS and restart Home Assistant. If your inverter still shows 0 W, check the site's approved export setting with your installer.
