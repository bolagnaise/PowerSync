<!-- release: v2.12.924 -->

## Tesla grid charging preference recovery

- Preserve the last observed or explicitly selected Tesla grid-charging
  preference when newer Powerwall firmware omits the setting from `site_info`.
- Restore the saved preference after Force Charge, Force Discharge, and
  optimizer Hold actions instead of assuming grid charging was enabled.
- Fail closed to grid charging disabled when no observable or remembered
  preference exists, including the mobile settings API.
- Accept Tesla's successful field-absent readback compatibility for direct
  controls as well as automations, while still rejecting malformed or failed
  commands.
