<!-- release: v2.12.834 -->

## What's Changed

### Reliable pre-export charge deadlines

Rolling optimizer updates now preserve the required battery SOC before priority export windows instead of granting a new feasibility allowance on every cycle. This prevents the charge target from progressively falling when execution is already slightly behind, while retaining the existing safety margin for reachable targets and respecting physical charge and grid-import limits.

Update available via HACS
