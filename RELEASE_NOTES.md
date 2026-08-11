<!-- release: v2.12.1067 -->

## What's Changed

**FoxESS restores now retry when remote control does not clear**

PowerSync now treats a failed FoxESS remote-control disable or restore write as
a real failure instead of recording the inverter as restored. Curtailment,
normal-mode, and Backup-mode transitions preserve their saved restore state
until every required write succeeds, allowing the next control cycle to retry
instead of leaving the inverter in VPP Controlled mode while PowerSync believes
normal operation was restored.

Update available via HACS
