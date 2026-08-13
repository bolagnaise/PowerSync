<!-- release: v2.12.1095 -->

## What's Changed

**Verify Tesla Smart Schedule charging starts**

PowerSync now waits for fresh, vehicle-specific charging state and measured current or power before treating an accepted Tesla Smart Schedule start request as physical charging. A vehicle already proven to be charging is recovered into the managed session without a redundant start. An unconfirmed new start receives an exact-vehicle compensating stop request, is replanned from current SOC, and retries with bounded backoff without leaving a ghost timer, session, or ownership lease.

Smart Schedule now also logs when its configured maximum is reduced by the active Tesla charger or vehicle limit. A configured 32 A maximum remains a ceiling: if the active charging path reports a 15 A limit, PowerSync safely plans and commands 15 A rather than silently overriding the live cap.

Update available via HACS
