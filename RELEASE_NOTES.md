<!-- release: v2.12.1824 -->

## Charge By Time plans no longer drop to "Optimizer degraded" with No Idle

With No Idle turned on and a Charge By Time target the battery could not fully reach, the optimizer could throw away a plan that would still charge most of the way. It then showed "Optimizer degraded" with no charging windows. This could happen when the plan landed a fraction of a percent short of the lower level it had settled on. PowerSync now keeps that charging plan, and the Charge By Time target is still shown as missed. Update through HACS and restart Home Assistant.
