<!-- release: v2.12.1328 -->

## What's Changed

**SolarEdge: a battery dispatch could be left on the inverter with no way back**

If Home Assistant restarted or the integration reloaded while PowerSync had a
battery dispatch running, the dispatch stayed on the inverter and PowerSync
could no longer undo it. Because a charge dispatch sets the discharge limit to
0 W (and a discharge dispatch does the same to the charge limit), the battery
could be left unable to serve the house at all — which looks exactly like the
inverter locking up after a mode change. Nothing in PowerSync could write those
limits back, so the only way out was physically power cycling the inverter and
battery.

Why it happened: PowerSync records the settings it changes on the inverter so it
can put them back. When Home Assistant starts and finds settings still recorded
as outstanding, it stops all SolarEdge control until a supervised check confirms
what state the hardware is in — the same protection used after a command whose
result could not be confirmed. But that block also stopped the one thing that
would have fixed it, the restore, and the supervised recovery service could only
ever confirm hardware that *already* matched the pre-dispatch settings. The
hardware was still holding the dispatch, so the service reported a mismatch and
refused, every time, including after another restart.

The protection stays, but it now has an exit. `power_sync.reconcile_solaredge_control`
reads the inverter's registers fresh and compares them against the values
PowerSync itself wrote, not just against the saved baseline. When every field it
owns still reads its own value — or has already returned to the saved value,
which is what happens once SolarEdge's command timeout expires — and nothing
else has moved, the service restores the saved settings and releases the block.
Charge and discharge limits, the command timeout and the grid-charge policy all
go back to what they were before the dispatch, and the battery works again
without touching the hardware.

If you are in this state now: call the service from **Developer Tools →
Actions** with `acknowledge: true` and your PowerSync entry ID. The
[SolarEdge control recovery](https://github.com/bolagnaise/PowerSync/wiki/SolarEdge-Control-Recovery)
page shows how to find that ID. A successful release reports
`released_owned_dispatch`.

Nothing writes to the inverter automatically. Anything PowerSync did not write
itself, a command whose result was never confirmed, and a new dispatch before
the block is cleared are all still refused, exactly as before.

Update available via HACS
