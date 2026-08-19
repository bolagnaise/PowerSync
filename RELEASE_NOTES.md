<!-- release: v2.12.1150 -->

## What's Changed

**Fixed: EV vehicle list failing for Sigenergy charger owners**
If you had a Sigenergy charger configured, the EV vehicle list would fail to load entirely — returning an error instead of your cars. The Sigenergy branch of the vehicle endpoint referenced a variable that did not exist in that scope, and because the error was raised while the list was still being assembled, it discarded every vehicle already found. Tesla and BYD owners who also ran a Sigenergy charger therefore lost their whole list, not just the charger entry. The endpoint now uses the correct stored reference, and a new check fails the build if this class of mistake reappears in any API view.

**EV charging planner: removed an unreachable code path**
The planner carried a "Smart Optimization drives EV charging" branch left over from the optimizer replacement in February. Every one of its three entry conditions was permanently false, so no charging session ever took it — the built-in planner has been handling all sessions. Removing it makes the planner's real behaviour clear and does not change how any vehicle charges.

Update available via HACS
