## What's Changed

**HA dashboard scene text now follows authored scene profiles exactly**
The previous patch still allowed legacy scene component fallbacks and runtime-only status text to bleed into generated layouts, which kept labels like grid and battery status misaligned versus the scene layout tool. Generated HA scene profiles now replace legacy profiles scene-by-scene, and battery/grid status text only renders when that scene explicitly defines positions for it.

Update available via HACS
