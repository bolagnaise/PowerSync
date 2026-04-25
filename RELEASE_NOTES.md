<<<<<<< HEAD
=======
## What's Changed

**Fix: Tesla Fleet API "user out of region" error blocks setup for EU and AP users**
EU and Asia-Pacific users connecting via Tesla Fleet API received a 421 "user out of region" error during setup, with no way to proceed — the integration silently returned "cannot connect." Tesla's 421 response includes the correct regional endpoint in the error body; the setup flow now detects this, parses the regional URL (e.g. `fleet-api.prd.eu.vn.cloud.tesla.com`), retries the validation against it automatically, and stores the correct endpoint in the config entry. All subsequent Fleet API calls — tariff syncing, TOU uploads, curtailment, AEMO spike mode, live status, force charge/discharge — now use the stored regional URL rather than hardcoding the NA server.

*Update available via HACS*
>>>>>>> d447d3d (Bump version to 2.12.170)
