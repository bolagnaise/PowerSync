<!-- release: v2.12.752 -->

## What's Changed

**Fix GoodWe export commands respecting the site export cap**
PowerSync now sends GoodWe EMS export commands using the planned site export target, capped by the configured Maximum grid export value. This fixes Smart Optimisation reissuing a larger GoodWe `sell_power` command when the optimizer had included household load in the battery discharge calculation, which could push export above the configured site/DNSP cap.

**Improve dashboard page scrolling over the action plan**
The Home Assistant dashboard action plan no longer blocks page mouse-wheel scrolling when the cursor is over the 24-Hour Action Plan list and the list itself has no scroll movement to consume.

Update available via HACS
