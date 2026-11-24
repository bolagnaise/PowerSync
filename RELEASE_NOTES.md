<!-- release: v2.12.1815 -->

## Grid charge blackout windows in Home Assistant

The blackout field now appears under **Smart Optimization → Grid & site constraints**. Use it to set the hours when PowerSync should block grid charging. Solar can still charge the battery during those hours.

## Tesla Boost and Solar Surplus

Boost can take over from a waiting Solar Surplus controller once fresh readings confirm the Tesla is stopped. If the handoff fails before Start is sent, PowerSync reports the reason and clears the temporary Boost record.

Solar Surplus now keeps standby draw from triggering repeated Stop commands for an idle Tesla.

Update through HACS and restart Home Assistant.
