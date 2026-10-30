<!-- release: v2.12.1565 -->

## Refreshed Energy flow readings and animations

The Energy flow card now uses the dashboard fonts and device colours, with readings directly on the scene and a soft glow for legibility. Grid import is cyan, and the grid reading turns red while exporting. Longer glowing energy streaks take their colour from the energy source and move faster at higher power. The old battery arrow and connector lines have been removed.

The 3D house and weather scenes are unchanged. The overlay uses plain gradients and strokes without blur filters.

## Clearer “Right now” and plan timings

The Right now headline now states what is happening without presenting the end of a five-minute optimizer step as the time the battery will run out. When the plan continues the current action for at least another 20 minutes, the card shows how long that planned run lasts. “Next it will…” now points to the next different action instead of repeating the current one. The plan card’s Now and Next chips also use the planned run and next real action change.

Update through HACS and restart Home Assistant. Hard-refresh the dashboard to load the updated cards.
