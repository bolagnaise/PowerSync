# Multiple vehicles on one generic charger

The EV Charging dashboard can store multiple vehicle profiles for the configured generic charger. Each profile has its own SOC entity, usable battery capacity and Smart Schedule settings, including target SOC and weekly departure times. The charger retains one meter, one controller and one charging-session identity.

In **EV Charging → Shared charger vehicles**, add each car with its SOC sensor and usable battery capacity. Before swapping cars, stop charging, unplug the car and wait for a fresh disconnected charger reading. Choose **Connected vehicle** before plugging the next car in. Expand Smart Schedule to set the selected car's target and departure times. These settings are restored when that car is selected again.

Selection is manual. The charger meter and connection-status entities must describe the physical charger, not one particular vehicle. PowerSync does not infer which car is connected from SOC values. Missing selection or unavailable selected-car SOC blocks Smart Schedule charging. The physical meter still contributes once to the EV/Home load split.

A vehicle cannot be switched or removed while charging, a command is pending, or charger recovery still owns the loadpoint. Stop/recovery records are preserved. Deleting an inactive profile does not remove another car's saved schedule. Existing single-car configurations retain their existing behavior until the first shared-charger profile is created.

## Implementation and verification plan

- Store vehicle metadata and Smart Schedule snapshots in the existing automation store; keep `generic_ev` as the physical runtime identity.
- Restore the selected snapshot on selection and startup, clear non-portable plans/caches, and source SOC only from the selected vehicle.
- Serialize profile mutations with the dynamic-start lock; reject active ownership, pending controls and stale/connected charger status. Fence starts queued with an older selected profile.
- Expose authenticated dashboard/API management and the existing connected-EV service. Keep charger controls and metering at charger scope.
- Verify two cars with different SOC/capacity/targets/departures, switching and reload, missing SOC/selection, command concurrency, failed persistence, deletion, single-car compatibility and meter deduplication. Run focused/adjacent regressions and the mandatory trusted full-suite release gate.
