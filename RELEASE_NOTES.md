<!-- release: v2.12.1549 -->

### Powerwall charging recovery

When fresh Powerwall readings show unreadable grid-charging permission, Local V1R and Automatic optimization now plan without unavailable grid refills. Solar charging remains available, and grid refills return when permission becomes readable. Tariff control keeps its existing charging checks.

A previous “Powerwall charging blocked” notification now clears after a later Tariff charge completes its command checks. Failed or unconfirmed attempts keep the warning; command confirmation does not establish physical charging. Update through HACS and restart Home Assistant.
