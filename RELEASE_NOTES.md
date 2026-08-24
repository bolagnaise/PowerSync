<!-- release: v2.12.1192 -->

## What's Changed

**Smart EV schedules now recover from delayed Tesla/TESSY stop telemetry**
After PowerSync stops charging during a Home Assistant restart or controller handoff, delayed cloud telemetry can briefly continue reporting the vehicle as charging. PowerSync now treats that interval as a provisional stop confirmation instead of permanently assigning the plug session to an external controller. Once stopped telemetry is confirmed, Smart Schedule can resume without requiring an unplug/replug.

Tesla app and vehicle-scheduled charging remain protected: automated modes stay command-neutral during the ambiguous interval, and genuinely external sessions retain external ownership.

Update available via HACS
