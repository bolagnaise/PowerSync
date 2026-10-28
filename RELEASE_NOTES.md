<!-- release: v2.12.1523 -->

## What's Changed

**Profit Max: store solar when delaying would lose value**

Profit Max could postpone solar charging because cheaper solar was forecast after an earlier valuable period for battery use. PowerSync now compares the complete charging plans and removes those delays when storing solar gives a better result, while keeping profitable direct solar export available.

Update through HACS and restart Home Assistant. Profit Max still does not guarantee a full battery by a deadline; use Charge By Time for that requirement.

Update available via HACS
