<!-- release: v2.12.1233 -->

## What's Changed

**Scheduled auto-update now retries HACS entity discovery**
If HACS is still creating its PowerSync update entity, or has not populated the
entity's install capability when the daily check begins, PowerSync now
rediscovers it on each retry instead of giving up for the day after the first
lookup.

**Auto-update diagnostics now refresh live**
The Auto-Update PowerSync switch now publishes each scheduler decision as it is
recorded. Its `last_check_at` and `last_check_decision` attributes immediately
show whether the scheduler is disabled, before or past its window, already ran
that day, or triggered the update check.

Update available via HACS
