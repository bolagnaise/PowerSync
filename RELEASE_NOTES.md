<!-- release: v2.12.1706 -->

## Manual battery holds survive optimizer cleanup

PowerSync now prevents optimizer cleanup from releasing an active manual Hold SoC. This fixes a reload case where GoodWe returned to normal operation while PowerSync still showed the manual hold as active, preventing EV battery preservation from taking over.

Manual holds continue to take priority over EV battery preservation. Update through HACS and restart Home Assistant. This correction prevents recurrence; it does not prove that an existing mismatched hardware state has recovered.
