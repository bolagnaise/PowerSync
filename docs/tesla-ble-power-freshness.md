# Tesla BLE charging-power freshness

## Vehicle presence and plug status

The ESPHome node's Wi-Fi connection does not establish that the vehicle is
connected over BLE. When both node Status and BLE Status exist, PowerSync requires
both to be on before using BLE readings as current plug or home evidence. Older
bridges exposing only one of these entities remain supported.

An unavailable native cable sensor and an expired idle charging-state report do
not establish that the car is plugged in. If native connectivity is also unknown,
an expired home location is no longer presented as confirmed home presence.
PowerSync shows unavailable status until new evidence arrives; connection loss
does not prove that the vehicle is away. Fresh idle reports and known cable-on
readings remain usable, including for sleeping vehicles.

When an explicitly paired bridge reports a newer BLE disconnect, expired Fleet
home, cable-on and Charging reports no longer establish current connection.
Fresh repeated reports still count even when their values have not changed.
The vehicle becomes unconfirmed rather than being declared away; possible stale
charging power stays unknown in EV/Home accounting. Expired Wall Connector
reports cannot restore that connection. A fresh connector report identifying
the exact vehicle can restore it.

## Charging-power readings

PowerSync accepts the optional raw charging receipt timestamps supplied by a
Tesla BLE bridge. This keeps confirmed steady power usable when Home Assistant
suppresses repeated equal numeric readings. The maximum measurement age remains
90 seconds.

For PedroKTFC bridges, enable the upstream
[charging freshness package](https://github.com/PedroKTFC/esphome-tesla-ble/blob/main/docs/charging-freshness.md)
in each bridge configuration, then rebuild and install its firmware. The bridge
must publish `sensor.<prefix>_charge_power_received` alongside
`sensor.<prefix>_charge_power`. Bridges using the `charger_power` alias must expose
its matching `charger_power_received` entity instead. Each configured vehicle
prefix has its own readings and receipt timestamps.

PowerSync uses the receipt entity's timestamp **state value**, not its HA update
time. A reconnect replay therefore cannot renew an old measurement. Missing or
invalid native measurements must not renew the receipt. Malformed, unavailable,
future, or timezone-free receipt timestamps are rejected; a fresh timestamp alone
cannot supply missing numeric power. The receipt only measures power freshness:
explicit stopped/completed states and command confirmations keep their existing
checks, including post-command vehicle readback.

If the matching receipt entity does not exist, PowerSync retains the existing
HA `last_reported`/`last_updated` behavior. Updating PowerSync alone cannot make
an older bridge provide raw receipt timestamps. HA `force_update` or a timer
which republishes cached data is not a replacement for a raw measurement receipt.
Use the unmodified native power sensor: custom filters that delay or suppress
valid values can separate the numeric reading from its raw receipt timestamp.

To verify, observe a steady charging session for more than 90 seconds: the
numeric power may stay unchanged, but the receipt timestamp must advance on
native samples and PowerSync EV/Home readings must remain available. When raw
power reports stop, the receipt must stop advancing and the old power must
expire. Test each bridge separately in a multi-vehicle installation.
