<!-- release: v2.12.751 -->

## What's Changed

**Fix scheduled charging for multiple Tesla vehicles**
PowerSync now starts every eligible Tesla vehicle during a Scheduled Charging window instead of relying on the default Tesla loadpoint after the first car starts. This fixes multi-Tesla setups where one home and plugged-in vehicle could begin charging correctly while another eligible vehicle was left idle because the shared scheduled charging state was already marked active.

Update available via HACS
