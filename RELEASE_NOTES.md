<!-- release: v2.12.1327 -->

## What's Changed

**Tesla BLE: Stop now works on a car that was already awake**
Pressing Stop on a Tesla controlled over BLE could fail with "Failed to stop
charging" while the car carried on charging — and it failed most reliably on
the car most likely to need stopping.

Before sending any BLE command, PowerSync wakes the vehicle and waits for the
bridge to confirm it is awake. That confirmation was only accepted if
`binary_sensor.<prefix>_asleep` was re-published at or after the moment the
wake button was pressed, with a two-minute-old observation as the only
alternative. A car that never went to sleep has no asleep transition to
report, so the ESPHome bridge publishes nothing, the acknowledgement can never
arrive, and after a full 30-second wait the stop was discarded before it was
ever dispatched. Since a charging Tesla is normally awake the whole time, this
was the ordinary case, not an edge case.

PowerSync already had a stricter standard for this exact situation, used for
rate adjustments: fresh, measured, *positive* charge current or power read from
the vehicle itself — not a writable control, not bridge status, not a stale
"Charging" label — and discarded if any newer explicit state says charging has
stopped, completed or disconnected. A stop now consults that evidence first and
dispatches straight away when it holds, with the substitution recorded in the
log. There is no 30-second wait to sit through either.

Nothing was loosened to achieve this. Starting a charge and setting a charge
limit still require the normal explicit wake acknowledgement, because neither
comes with a measured draw to corroborate. The post-dispatch readback is
unchanged, so an accepted service call still never counts as a confirmed
physical stop, and the wake backoff and its user-command bypass behave exactly
as before.

**Solar Surplus: an unavailable EV reading no longer reads as zero demand**
The debug log could show two different answers for the same instant — 
`Surplus calc (grid_based): ... ev=7.68kW ... available=8.94kW` alongside
`ev=0.00kW ... available=1.26kW` a millisecond apart.

The grid-based surplus formula adds the EV load back in because the grid meter
has already netted it off. The Solar Surplus control loop handles a momentarily
unavailable EV power reading by falling back to the power it commanded, but the
EV display snapshot and the loadpoint status API treated the same missing
reading as a measured 0 kW — understating the reported surplus by the entire EV
draw. Both now use the control loop's fallback.

Charging decisions were never affected: every allocation in the reported window
came from the control loop and was correct. What was wrong was the surplus
figure published to the display and API paths, which is what made the log look
as though PowerSync kept losing track of the car.

Update available via HACS.
