<!-- release: v2.12.1569 -->

## See solar charging separately in the optimizer plan

The dashboard now labels solar charging separately from charging that uses the grid, so an earlier solar charge no longer appears to contradict the next planned control action. Mixed charging windows show the grid-backed energy, and their estimated cost applies only to that share.

## See the Super Export bonus allowance

The optimizer plan now shows the configured daily Super Export bonus cap and the allowance remaining today. Export earnings use the available premium allowance shared with solar export; the cap applies to premium-rate energy, so total planned export can be higher.

Update through HACS, restart Home Assistant and refresh the dashboard. This update corrects the display; it does not introduce a new export limit.
