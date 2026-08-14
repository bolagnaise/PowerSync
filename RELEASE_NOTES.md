<!-- release: v2.12.1101 -->

## What's Changed

**Only report confirmed FoxESS optimizer force actions**

FoxESS force-discharge failures now propagate through the Home Assistant service path instead of being acknowledged as successful. H1/H3/KH systems restore their saved work mode and minimum SOC after an unconfirmed charge or discharge transition, while H3-Pro/H3-Smart systems safely clear remote control without rewriting work mode. If cleanup cannot be confirmed, PowerSync retains the saved baseline so a later cycle can retry rather than discarding recovery state.

**Keep planned actions separate from acknowledged hardware status**

The Action Plan still shows the optimizer's intended Charge or Export slot, but the current and effective action now remain on the last acknowledged hardware action—or the applicable safe runtime default—when execution is unavailable, blocked by Monitoring Mode, or fails before confirmation. This prevents the dashboard from claiming Charge or Export solely from an unexecuted plan while preserving No Idle and demand-window safety behavior.

Update available via HACS
