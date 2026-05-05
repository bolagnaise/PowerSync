<!-- release: v2.12.288 -->

## What's Changed

**Stabilise Bottlecap Dave Saving Sessions**
PowerSync now normalises Saving Session events from the Bottlecap Dave Octopus Energy integration before handing them to the optimiser. Joined events with a missing `octopoints_per_kwh` value now use the default Octopus reward rate instead of crashing or being treated as malformed.

**Use auto-joined sessions immediately**
When auto-join succeeds through the Octopus Energy service, PowerSync now exposes the newly joined event to the next optimiser run without waiting for Dave's event entity to refresh. Session parsing also de-duplicates and sorts events so the active and next-session views are deterministic.

**Harden optimiser session overlays**
The optimiser now tolerates null reward values, invalid octopoints conversion settings, and naive datetimes when applying Saving Session price boosts. Free Electricity still affects import pricing only and does not enable battery export.

Update available via HACS
