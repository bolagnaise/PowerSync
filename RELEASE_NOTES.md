<!-- release: v2.12.1666 -->

## Correct Home Load with a Tesla Wall Connector

Fixed a case where one Tesla appeared twice while charging through a Home Assistant Wall Connector, causing Home Load to read zero. The connector's vehicle-current sensor can no longer be mistaken for a vehicle identifier, so the existing single-car matching can count its charging power once.

This corrects new readings; previously recorded Home Load history is not rewritten. Update through HACS and restart Home Assistant.
