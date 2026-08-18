<!-- release: v2.12.1138 -->

## What's Changed

**EV Smart Schedule plans now match executable charging policy**
Cost Optimized and Solar Preferred plans now apply each vehicle's maximum grid price before publishing charging windows, so an expensive window that Smart Schedule would refuse at execution time can no longer appear as current EV demand in the optimizer. Cached plans receive the same defensive projection check, while Time Critical vehicles continue to use paid pre-departure charging when required.

Free-window planning now honors the vehicle's live minimum charge current and whole-amp control steps, retains feasible residual site capacity instead of dropping it at a fixed 1.4 kW threshold, and accounts for capacity already reserved by another EV. Plans that cannot reach the requested target inside the allowed windows now report the energy shortfall instead of adding an unexecutable paid window. Partial charging windows are also trimmed to their declared energy so optimizer forecasts no longer overstate EV demand.

Update available via HACS
