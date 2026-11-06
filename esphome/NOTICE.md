# Bridge component licensing and source

The components in this directory are distributed under **AGPL-3.0-only**;
see [LICENSE](LICENSE). They are separate from the Home Assistant integration.

`tesla_ble_vehicle` and `tesla_ble_listener` derive from
[PedroKTFC/esphome-tesla-ble](https://github.com/PedroKTFC/esphome-tesla-ble/tree/651b4c25dcb24ce8d16d625a51868576e83bf2fe),
commit `651b4c25dcb24ce8d16d625a51868576e83bf2fe`.
The original authors' source and license are preserved. PowerSync modifications
add an optional producer-owned Stop recovery protocol; they do not attest that
an existing firmware binary already supports that protocol.
