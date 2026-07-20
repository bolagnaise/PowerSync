<!-- release: v2.12.896 -->

## What's Changed

**Flow Power now uses the supported Web Data API only**
PowerSync no longer signs in to the Flow Power customer portal, performs SMS MFA, stores portal credentials or cookies, scrapes reports, or falls back to automated portal requests. Flow Power prices and account summaries now come only from the API key available in the Flow Power app under **More > Web Data Access**.

**Existing Flow Power setups migrate safely**
Upgrading removes legacy portal credentials and session data. Portal-only setups continue with AEMO-direct pricing and receive a Home Assistant repair explaining how to add the Web Data API key; existing account sensor entity IDs are preserved, and valid price-only keys remain supported without account-summary sensors.

Update available via HACS
