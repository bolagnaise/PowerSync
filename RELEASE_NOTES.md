<!-- release: v2.12.322 -->

## What's Changed

**Dashboard tile reordering is easier to use**
Customize layout now uses a single pointer-based drag method with a visible drop placeholder. Tiles reflow while you drag, empty column space can accept drops, and the saved layout only stores real dashboard tiles, which makes repositioning much more predictable.

**Dashboard frontend cache refresh**
The bundled dashboard JS resource version was bumped so Home Assistant requests the updated strategy file after the HACS update instead of reusing the previous drag implementation from browser cache.

Update available via HACS
