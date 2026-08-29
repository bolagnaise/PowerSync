<!-- release: v2.12.1205 -->

- Add optional site-local Smart Optimization grid-charge blackout windows. Windows can cross midnight and remain correct across timezone transitions.
- Blackouts prevent optimizer force charging while leaving solar charging, normal self-consumption, and normal battery operation unchanged.
- Preserve Charge By Time targets and report when blackout windows uniquely make a target infeasible.

Update available via HACS.
