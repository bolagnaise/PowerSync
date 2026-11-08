# Inspect retained Tesla native commands

An earlier Tesla Fleet, Teslemetry or Tessie command can have an unknown outcome
after a lost response. PowerSync retains that attempt and blocks conflicting
Start, current and charge-limit requests. A later successful Stop does not replace
the earlier attempt's outcome.

Finite manual controls now report a retained native-command hold separately from
a producer binding change, an unavailable/disabled entity, or invalid recovery
history. These diagnostics preserve the existing command restrictions.

## Read-only inspection

After updating through HACS and restarting Home Assistant, run **Developer Tools
→ Actions → PowerSync: Inspect Tesla EV recovery status**:

```yaml
action: power_sync.get_ev_recovery_status
data:
  entry_id: YOUR_POWERSYNC_ENTRY_ID
  vin: YOUR_UPPERCASE_17_CHARACTER_VIN
```

Specify the PowerSync entry when multiple entries are loaded. The response covers
only the requested VIN, with original attempt IDs, operations, deadlines, producer
identities, generation and journal revision. Missing/unloaded history is reported
as unavailable rather than an empty history. The action reads existing HA data;
it does not refresh, wake, send controls, migrate records, or clear a hold.

New native current/limit attempts retain `route.command_request` with the exact
HA service value, operation and unit before dispatch. The immutable attempt digest
covers this metadata. This records a request, not a measured vehicle setting or
a qualified physical effect bound: providers and vehicles may transform values.
Older records retain their original missing metadata and deadlines unchanged.

## What is still required for recovery

This change does **not** unlock older unknown native commands. Current stopped
telemetry, an accepted later Stop, an HA restart, or a newly observed requested
current does not establish that the original operation cannot act later.

Tesla recommends checking vehicle state after a timeout and notes that most
stateful commands can be retried. That guidance does not define an ordering or
drain guarantee for previously accepted work. The inspected public charging API
does not expose a general command-history, cancellation or application-drain
contract. See [Tesla API best practices](https://developer.tesla.com/docs/fleet-api/getting-started/best-practices)
and [vehicle commands](https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-commands).

A safe retirement path requires correlated terminal evidence for the original
attempts, or a supported provider lifecycle/drain guarantee covering all possible
submissions and later effects. The original generic exception class alone cannot
supply this proof. Repeating a customer log capture does not create that guarantee.

An independently enforced EVSE ceiling could instead contain an uncertain Rate
without retiring it. That requires an exact VIN/charger association, durable
enforcement, persistent site/per-phase reservations for every retained effect,
and compatibility with the requested charging policy as it changes. A fixed
charger maximum alone does not establish a lower Solar Surplus allowance.
There is no general containment override in this release.

Automatic location/plug eligibility remains separate. Correcting a transport
diagnostic does not turn an away or unknown vehicle into an eligible home vehicle.

The regression checks prove durable request recording, accurate refusals,
read-only exact-entry inspection, legacy-record preservation and unchanged
priority Stop/finite cleanup boundaries. Publication does not establish reporter
installation, vehicle recovery, or physical charging.
