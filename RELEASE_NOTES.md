<!-- release: v2.12.863 -->

## What's Changed

**Generic charger commands are idempotent across every control path**

PowerSync now skips duplicate start commands when a generic charger switch is already on, including direct starts and solar-surplus handoffs. Generic wrappers around HACS OCPP retain the required off-to-on recovery when the connector is in `Finishing`, with time for the switch state to settle before restart.

**Generic charger stops no longer depend on a zero-amp helper value**

Switch-backed generic chargers now stop through their configured switch without first forcing the optional amperage entity to `0`. Amps-only chargers still use the configured `number.*` or `input_number.*` service domain.
