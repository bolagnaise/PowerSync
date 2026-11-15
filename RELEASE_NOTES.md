<!-- release: v2.12.1712 -->

## Tesla battery output and persistent site limits

Paired Tesla gateways now have separate controls for timed battery-output targets and persistent native import/export limits. Battery output uses positive watts to discharge and negative watts to charge; existing Force controls keep their current meanings. Zero-power hold is not available.

Persistent limits require configuration readback and explicit installation/network approval before increases. They stay in place after timed controls stop. An uncertain write blocks further acquisition until reconciliation; command acceptance and configuration readback do not verify physical tracking or limit enforcement on every firmware.

## Matching planning and automations

The optimiser and existing managed EV admission use restrictive recorded site limits, including zero. Versioned automation actions use the dedicated controls, retain uncertain requests without replay, and allow later triggers to recover after a refused request. Existing manual EV charging policy is unchanged.

Update through HACS and restart Home Assistant. Use the matching current mobile release for the new mobile controls.
