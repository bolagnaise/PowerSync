<!-- release: v2.12.1736 -->

## Tesla Bluetooth charging power during recovery

The Energy flow card now shows EV charging separately from Home when a connected Tesla Bluetooth bridge supplies fresh, matching charging and power readings during a command recovery hold. PowerSync continues to require recovery before sending new charging commands.

Update through HACS and restart Home Assistant. Existing bridge firmware that already supplies paired readings can be used as is.
