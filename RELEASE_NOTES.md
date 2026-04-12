## What's Changed

Follow-up to 2.11.16 caught by the first successful pairing test on real hardware. The Tesla Fleet API handshake worked end to end — the RSA key was auto-verified because the user had toggled the DC isolator a few minutes earlier and Tesla's cloud honours a short physical-presence grace window — but three issues prevented the local client from actually talking to the gateway afterwards.

**Fix: SSL context construction no longer blocks the HA event loop**
`ssl.create_default_context()` internally calls `load_default_certs()` which HA 2025's blocking-call detector flags because it reads from disk. The transport class was building a fresh context on every call to `_insecure_ssl_context()` directly from the event loop. Refactored into a module-level cache plus a new `get_insecure_ssl_context(hass)` helper that dispatches the initial build through `hass.async_add_executor_job`. Subsequent calls return the cached context instantly. The coordinator warmup now primes the cache before constructing the client, so the first pairing attempt is clean.

**Fix: `PowerwallLocalCoordinator` now accepts a `ConfigEntry`**
Modern HA `DataUpdateCoordinator` requires `config_entry=` in `__init__` otherwise `async_config_entry_first_refresh()` raises *"only supported for coordinators with a config entry"*. The coordinator now takes the full entry instead of just its id, passes it to the base class, and falls back to the legacy signature if the kwarg isn't supported (older HA installs). The first-refresh warmup in `ensure_coordinator()` now works as intended.

**Fix: Customer password is required, not optional, on Powerwall 3**
The pairing wizard previously marked the customer password as "(optional for PW3)" — that was wrong. Both Powerwall 2 and Powerwall 3 use Bearer-token auth on the REST endpoints the integration depends on (`/api/meters/aggregates`, `/api/system_status/soe`, `/api/v2/islanding/mode`), and the Bearer token comes from `POST /api/login/Basic` which requires the last 5 characters of the Gateway serial as the password. Without it the gateway returns `401 bad credentials`, pairing reports success (because Tesla's cloud accepted the RSA key), but live monitoring and the Go Off-Grid button both silently fail at runtime. The mobile wizard now marks the field as required, blocks submission when it's blank, and explains where to find the serial (sticker on the side of the gateway, or Tesla app → Products → your Powerwall → Gateway).

Update available via HACS
