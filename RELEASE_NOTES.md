<!-- release: v2.12.1155 -->

## Price Level again stops a car that starts charging itself at a high price

**Update if you run Solar Surplus and Price Level together and your car starts charging on its own when you plug it in.**

Plug a Tesla in after dark and the car may start charging by itself — nothing to do with PowerSync. Price Level already had a policy for that: if it is enabled and has decided *not* to charge, and it finds the car charging anyway, it sends a stop. That is how PowerSync enforces your price threshold against the car's own auto-start.

That policy was being skipped whenever Solar Surplus was also switched on.

When you plug in with Solar Surplus enabled, PowerSync opens a Solar Surplus session immediately, before it knows whether any surplus exists. After dark that session commands nothing — it allocates 0 A and never sends a start. When it then sees the car drawing power it hasn't asked for, it logs `detected external manual start … suspending automated rate control` and deliberately steps back, leaving the charge alone.

At that point nothing in PowerSync was governing the car. Solar Surplus had suspended its own control, but it still held the ownership lease on the loadpoint — so every 30 seconds Price Level looked at an 8 kW charge it had already decided against, saw `solar_surplus mode owns the active session`, and left it running. Reported from a site where that ran for seven minutes with zero solar and near-zero grid, so the house battery carried the whole 8 kW until the charge was stopped by hand.

A mode may now only hold a loadpoint against other modes for as long as it is actually governing it. Once a Solar Surplus session has suspended its own rate control for an externally started charge, it no longer masks Price Level's decision — and no longer blocks the resulting stop command either, which is the second half of the same bug.

Unchanged:

- A Solar Surplus session that is actually setting the charge rate still owns the loadpoint, and Price Level still leaves it alone.
- A session waiting for sustained surplus before it allocates amps also still owns the loadpoint. Price Level does not poach it mid-day.
- Solar Surplus still leaves an externally started charge alone rather than fighting it for rate control. That behaviour is intentional and is not what changed.

**What you will notice:** with both modes enabled, a charge that starts on its own while the price is above your Price Level threshold now gets stopped, exactly as it already did for anyone running Price Level without Solar Surplus.

Your battery reserve floor was not involved in the reported case — the site was above its 76% floor the whole time, and the floor would have ended the battery-fed portion at 76%.
