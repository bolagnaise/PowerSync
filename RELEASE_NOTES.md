<!-- release: v2.12.900 -->

## What's Changed

**Early-morning Day summaries now start at today’s reset**
PowerSync no longer attributes Home Assistant’s synthetic pre-midnight recorder state to the current day. This fixes the remaining case where the mobile Day view could show yesterday’s terminal totals after all live daily sensors had reset.

**Real recorder history remains intact**
Only the synthetic state before the requested range is excluded. Genuine in-range readings, same-day accumulation, transient restore states, and multi-day resets keep their existing handling.

Update available via HACS
