<!-- release: v2.12.374 -->

## What's Changed

**Optimizer reserve saves now survive restart cleanly**
PowerSync now keeps the LP optimizer backup reserve in sync across both Home Assistant config stores when it is changed through the app or API. This fixes systems where startup could still load an old 45% optimizer floor after the user had saved 20%, causing PowerSync to avoid battery discharge and import from the grid even though the GoodWe hardware reserve was correctly set to 20%.

Update available via HACS
