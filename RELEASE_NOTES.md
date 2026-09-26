<!-- release: v2.12.1335 -->

## What's Changed

**Preserve FoxESS battery inputs independently**

Manual capacity, charge-power, and discharge-power settings now retain ownership per field. FoxESS live power detection fills only unset fields, and the settings API exposes each field's source so mixed manual/automatic configurations remain visible.

This corrects optimizer inputs without changing the existing export valuation policy.

Update available via HACS.
