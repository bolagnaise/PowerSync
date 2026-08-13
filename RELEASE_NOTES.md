<!-- release: v2.12.1083 -->

## What's Changed

**Release home-battery holds when Smart Schedule is not charging**
Preserve Home Battery now blocks discharge only while Smart Schedule is actively charging an EV. An away, unplugged, disabled, or deleted vehicle can no longer keep the Powerwall reserve pinned to its current state of charge, and No Idle correctly returns to self-consumption after the EV hold clears.

**Keep accepted Tesla force charges active when tariff readback is delayed**
Tesla force charge now distinguishes a rejected tariff upload from a tariff Tesla accepted but did not immediately echo through `site_info`. Accepted commands continue with bounded degraded-state tracking and guaranteed restore cleanup instead of rolling back, retrying every optimiser cycle, and sending a false failure notification.

Update available via HACS
