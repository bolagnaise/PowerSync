<!-- release: v2.12.1827 -->

## The optimiser now says why a planned charge or export isn't happening

When the optimiser planned a charge or export but the battery command was refused, the dashboard only showed the battery in self-consumption next to the plan. The reason was only in the logs. The plan card now shows a **Charge blocked** or **Export blocked** chip with the reason, such as **Gateway unreachable** when the Powerwall gateway isn't answering on your network, or **EV reservation held** when a car's charging reservation is stopping cloud tariff charging. Hover over the chip to see the full message. The Current Action sensor has a matching `command_failure` attribute you can use in automations. It clears as soon as the next command succeeds.

For paired Powerwalls, the local status that the mobile app reads now says whether its kW limits come from a current gateway reading. A mobile app update that uses this to say "gateway not responding" instead of "no available capacity" is on the way. Update through HACS and restart Home Assistant.
