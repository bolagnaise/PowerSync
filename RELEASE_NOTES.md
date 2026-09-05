<!-- release: v2.12.1235 -->

## What's Changed

**Zero feed-in batteries no longer drain to the reserve floor before a same-price recharge**
PowerSync already refuses to discharge and then buy the same energy back when
the later recharge is not cheap enough to repay round-trip losses, but that
check only applied when the battery was more than 5 percentage points above its
backup reserve. Just above the reserve the check was skipped, so the plan drained
the battery to its floor and then grid-charged inside the same price window — a
pure loss. The round-trip check now applies at any state of charge above the
reserve, and an economically justified hold is no longer remapped to
self-consumption when the command is executed. Behaviour at or below the
optimiser reserve is unchanged.

**One Tesla on a BLE bridge and its Wall Connector now shows as one car**
On a site whose only Tesla is read through a local ESPHome Tesla BLE bridge, the
Wall Connector was published as a second vehicle. That double-counted the car's
charging power, which in turn subtracted twice from Home Load — showing 0 W of
household load — and inflated the reported solar surplus. The Wall Connector now
collapses into a BLE-only Tesla the same way it already collapsed into a
Fleet/Teslemetry Tesla. Sites with a second car, or a separate non-Tesla charger,
still show separate loadpoints.

**Sigenergy Solar Surplus charging reads the charger's own meter**
Sigenergy exposes charge and discharge power *limit* entities but no power
*meter* entity, so Solar Surplus control had no measured EV power and fell back
to the last commanded current. After a reload that command is zero while the
charger keeps delivering, so the calculated surplus excluded the car's own draw,
never cleared the minimum charging current, and left the site exporting while the
EV sat at its 6 A floor. Solar Surplus now reads the Sigenergy charger's measured
power directly, so an already-running charge is seen and can be ramped. An
unavailable reading stays unavailable rather than being treated as a measured
zero.

Update available via HACS
