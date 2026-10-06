<!-- release: v2.12.1352 -->

## What's Changed

**Restore profitable export after below-reserve recovery**

When battery SOC is just below the optimizer reserve, later export planning now
uses the effective acquisition cost from eligible earlier charging slots. This
keeps reserve recovery and charge eligibility safeguards intact while allowing
profitable lower-FIT export after a supported cheap charging window.

**Regression coverage**

Added coverage for near-reserve SOC with positive acquisition cost, eligible
cheap charging, and later lower-FIT export.

Update available via HACS
