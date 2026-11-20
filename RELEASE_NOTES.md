<!-- release: v2.12.1756 -->

## Startup when Tesla Fleet is unavailable

PowerSync now starts independently of Home Assistant’s Tesla Fleet integration. If Tesla Fleet fails to load, PowerSync entries that use other providers can still start. Entries that use Tesla Fleet for their Powerwall connection retry when credentials are unavailable.

Update through HACS and restart Home Assistant. If Tesla Fleet still reports a setup error, resolve that error to restore the features that rely on it.
