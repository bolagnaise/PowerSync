<!-- release: v2.12.1676 -->

## GoodWe controls through Home Assistant

Supported GoodWe hybrids using GoodWe Experimental Home Assistant telemetry now support verified charge, discharge, Hold SoC, normal operation and on-grid backup reserve through their existing connection. PowerSync also supports export limiting and the optional BuyPower load-following method on this route, without opening a second TCP connection.

## GoodWe curtailment and recovery

Choose **GoodWe connection → Solar curtailment method → EMS load following (BuyPower 0 W)** to try native load following when a zero-watt export limit is accepted but export continues. PowerSync saves the original settings before changing them, restores them after interruptions, and avoids overwriting settings changed by another controller. Missing capabilities or a mismatched device keep control Pending and produce a diagnostic warning.

Load following requires suitable whole-site GoodWe metering and compatible firmware. These paths are verified in software; physical export containment on ESA/EHA-G20 remains unverified. Keep other EMS-writing controllers from competing with PowerSync.

Update through HACS and restart Home Assistant. Keep the existing working GoodWe connection; entity control no longer requires a separate IP address in PowerSync.
