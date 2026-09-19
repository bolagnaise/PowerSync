<!-- release: v2.12.1320 -->

## What's Changed

**Tesla Powerwall: prevent incorrect reserve conversion after local pairing**
PowerSync could compare a current local reserve with an older Tesla cloud
setting and infer the wrong hidden-reserve offset. A later request could then
write a different reserve from the one requested and falsely confirm success
by reading back its own converted value. Matching pending writes could also
keep that incorrect conversion authoritative after cloud data disagreed.

Backup reserve requests now use Tesla's existing cloud API in the user-facing
percentage scale, with a separate matching site-info readback required for
success. This applies to the shared manual, optimizer, hold and restore paths.
Unconfirmed requests remain failures; existing ownership, Monitoring Mode,
serialization and restore/retry safeguards remain in place.

**Cloud access is required for backup reserve changes**
Local pairing no longer enables offline backup reserve changes. If Tesla's
cloud is unavailable or its readback does not match, PowerSync cannot confirm
the reserve change. Other local telemetry and controls are unchanged.

PowerSync also stops displaying guessed local reserve values or treating a
pending local target as a live measurement. Previously inferred offsets and
pending conversion state are discarded. Cloud readings keep their freshness
classification; raw local configuration remains available for diagnostics.
Existing saved user reserve preferences are not automatically rewritten.

Focused regressions cover stale and missing cloud data, first-write bootstrap,
previously corrupted offsets, contradictory pending state, non-default offsets,
0/100% targets, separate sites, and accepted-but-unconfirmed cloud writes.
This corrects a reproduced code defect; it does not establish the cause of
any particular customer's physical behavior or resolve unknown grid readback.
Update through HACS, restart Home Assistant, and check your intended reserve
before retesting if you previously experienced unexpected settings.

Update available via HACS
