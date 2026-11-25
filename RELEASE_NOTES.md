<!-- release: v2.12.1841 -->

## Sungrow battery hold no longer logs a rejected write every cycle

On some Sungrow inverters, every optimizer cycle that holds the battery logged `Modbus write error at register 33047` and `Failed to set discharge rate limit`. These inverters reject a 0 W discharge limit but accept their 10 W minimum, which PowerSync already used immediately afterwards, so the hold itself was working. PowerSync now remembers the rejection and uses 10 W directly for later holds. The normal discharge limit is still restored when the hold ends.

Update through HACS and restart Home Assistant.
