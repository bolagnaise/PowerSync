<!-- release: v2.12.1098 -->

## What's Changed

**Keep paired Tesla BLE Smart Schedule sessions on one physical vehicle**

PowerSync now coalesces a Tesla's Fleet or Teslemetry VIN and its paired ESPHome BLE alias into one Smart Schedule profile and runtime state. Existing VIN settings remain authoritative, alias-only settings and cached state of charge migrate safely, and the paired identity can no longer start a second controller that stops the same charger while the VIN plan still wants charging.

**Show the real Smart Schedule stop reason**

EV notifications now report the actual decision that stopped charging, such as insufficient three-phase solar surplus, a reached target, a battery floor, or Smart Schedule being disabled, instead of labeling every stop as an ended schedule window.

Update available via HACS
