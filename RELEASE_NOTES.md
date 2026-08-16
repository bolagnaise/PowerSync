<!-- release: v2.12.1113 -->

## What's Changed

**Stopped externally started Tesla sessions when one provider is stale**
Smart Schedule no longer treats a stale Tesla `stopped` state as proof that an independently detected charging session has already ended. PowerSync now continues through the configured vehicle-specific stop path, while retaining the existing away-location protection, so fresh Wall Connector draw cannot be acknowledged as stopped without a stop request being sent.

**Prevented stale Tesla plug states from temporarily forcing a 5 A charging cap**
When duplicate VIN-scoped Tesla integrations disagree during plug-in or unplug propagation, PowerSync now uses the newer state only when it is at least 60 seconds fresher than the contradiction. This allows a fresh paired Teslemetry or TeslaBLE observation to expose the real active-charger limit instead of briefly falling back to the conservative 5 A unknown-charger cap.

**Kept ambiguous charger associations fail-closed**
Missing timestamps and recent provider disagreements still use the existing safe 5 A fallback. New regressions cover stale stopped telemetry, stale plug conflicts, near-simultaneous ambiguity, and a fresh unplugged state winning over stale plugged data.

Update available via HACS
