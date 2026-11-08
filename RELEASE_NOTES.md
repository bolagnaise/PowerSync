<!-- release: v2.12.1660 -->

## Optional read-only support diagnostics

Home Assistant administrators now have a Support card for downloading selected diagnostic values and optionally recording recent history. Recording is off by default. Diagnostic capture reads cached state and does not refresh hardware or change energy settings.

Remote investigations require a separately configured support service, which ships disabled. When available, access requires your approval of the named investigator, ticket, data scope and processing policy, expires automatically and can be revoked in HA. Sharing a retained incident archive requires separate approval.

This feature provides no customer AI chat and needs no native mobile setting. Update through HACS, restart Home Assistant and refresh the dashboard.
