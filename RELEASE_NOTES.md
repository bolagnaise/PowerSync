<!-- release: v2.12.1554 -->

## What's Changed

### Zaptec charging-rate recovery

When a standalone Zaptec current update fails or is rate-limited, PowerSync now keeps the last acknowledged rate and retries on later control cycles. This prevents charging from remaining at a reduced rate because a rejected increase was treated as successful.

The site import limit and 15-minute interval between installation-current updates still apply. Update through HACS and restart Home Assistant.
