<!-- release: v2.12.1555 -->

## What's Changed

### Zaptec Price-Level charging after a stop

Standalone Zaptec charging no longer stays blocked waiting for stop confirmation after a fresh charger poll confirms that charging has stopped. This allows an eligible Price-Level charge to start after PowerSync stops an earlier session, including after a Home Assistant restart.

Older or uncertain readings continue to block confirmation, and separately controlled charging sessions remain protected. Update through HACS, restart Home Assistant and retest one Price-Level charging cycle. Zaptec authorisation and charging limits still apply.
