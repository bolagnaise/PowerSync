<!-- release: v2.12.1231 -->

## What's Changed

**Amber plans now refresh when a new settled billing interval arrives**
PowerSync distinguishes a genuinely new current price interval from duplicate
usage and spot-price callbacks. A settled Amber interval received shortly
after the preceding optimizer solve now refreshes the plan and its displayed
price generation instead of retaining the prior interval until the cooldown
expires. Duplicate updates for the same interval remain coalesced to avoid
repeated control commands.

**Tesla BLE unavailable power remains unknown in EV status**
The EV loadpoint endpoint now carries Tesla BLE power availability and measured
current through its status projection. An unavailable reading is shown as
unknown rather than being converted into a measured `0.00 kW` Idle state; an
explicit zero-power observation remains a valid idle reading.

Update available via HACS
