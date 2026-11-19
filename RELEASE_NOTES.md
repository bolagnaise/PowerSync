<!-- release: v2.12.1749 -->

## Powerwall local dispatch renewal

A temporary slowdown reading the gateway could end a local charge or discharge early. PowerSync now makes one more read if the first sample is already too old. It still needs fresh readings and enough time left in the original action to renew dispatch.

Update through HACS and restart Home Assistant.
