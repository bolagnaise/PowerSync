<!-- release: v2.12.1689 -->

## Optional HA diagnostics for Codex and Claude

Home Assistant administrators can now approve ongoing read-only diagnostics separately for Codex and Claude Code. Coverage includes entities, registries, configuration, automations, scripts, dashboards, traces, history, statistics, logs and system metadata. Administrators can revoke one approval or all installation access from the HA diagnostics card.

This is an operator-configured pilot and stays disabled after updating. Each client and each access purpose requires separate owner approval. Bridge packages and the setup guide are included with this release; optional sources and unsupported Home Assistant versions are identified explicitly.

## Private optimizer regression

A separate approval can contribute fresh optimizer captures to private baseline/candidate replay and calibrated simulation. Raw captures are replayed and deleted, with bounded summaries available to the approved client. Unconfirmed cleanup blocks further participation until deletion is confirmed.

The service includes stateful reserve-restoration checks and advisory fleet reports. Real-system calibration and seven elapsed observation days are still required for pilot acceptance; synthetic tests do not establish physical behavior or real savings. Update through HACS and restart Home Assistant to load the new software.
