<!-- release: v2.12.1711 -->

## Tesla Fleet charging starts

PowerSync now waits for a queued Tesla Fleet refresh before checking whether charging can start. This avoids refusing a start simply because Home Assistant returned before that refresh finished; vehicle identity and fresh-data checks still apply.

Update through HACS and restart Home Assistant.
