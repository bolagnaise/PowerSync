<!-- release: v2.12.862 -->

## What's Changed

**Stopped duplicate generic charger start commands**
Generic charger current updates now skip `switch.turn_on` when the configured charger switch is already on. This prevents OCPP charge-control switches used through Generic charger mode from receiving repeated RemoteStartTransaction requests and flooding Home Assistant with rejection notifications.

**Fixed generic current control through input helpers**
Generic charger amperage writes now use the configured entity's actual `number` or `input_number` service domain. Configurations using an `input_number.*` helper now apply the requested current instead of silently calling the wrong service.

Update available via HACS
