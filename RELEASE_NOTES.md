<!-- release: v2.12.1206 -->

## What's Changed

**Tesla BLE vehicles load consistently in mobile EV views**
PowerSync now combines legacy configuration data with current options when resolving the EV provider and BLE entity prefix for mobile EV status, vehicle lists, automation pickers, and vehicle commands. Existing installations that keep Tesla BLE settings in their original config entry can therefore show the same BLE vehicle across the planner and mobile views; newer options still take precedence.

Update available via HACS
