<!-- release: v2.12.1175 -->

## What's Changed

**Spread Import now keeps the scheduled charge rate on Sigenergy**
When Spread Import is enabled, PowerSync now sends the rate calculated for the
spread window instead of raising it back to the maximum available free-import
headroom. Live site headroom still reduces the command when needed to stay
within the configured import limit.

Update available via HACS
