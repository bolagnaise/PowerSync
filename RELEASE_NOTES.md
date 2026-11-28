<!-- release: v2.12.1871 -->

## Smart Schedule keeps charging when a free-grid window ends

When a free (0¢) import window ended and Smart Schedule switched a charging Tesla to solar, it stopped the car and started it again. On a Bluetooth bridge the stop often couldn't be confirmed in time, so the car stayed stopped with "Charging control paused" even though spare solar was available.

Now, when the car is charging and controlled over Bluetooth, Smart Schedule hands the running charge to its solar controller without stopping it, and charging continues from the current rate. Switching from solar back to grid, and cars controlled through a pinned Tesla Fleet or Teslemetry route, still stop and restart as before.

Update through HACS and restart Home Assistant.
